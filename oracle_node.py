#!/usr/bin/env python3
"""
Usage:
    python3 oracle_node.py <config_file>
"""

import sys
import os
import socket
import select
import struct
import time

MSG_CONNECT = 0x01   # VN to ON:type-1 ip-4 udp-2
MSG_LINK_STATE = 0x02   # ON to VN:tyoe-1 n-1 (nodeip-1, ip-4, udp-2, cost-2)s

CONNECT_BODY_LEN= 6         
TUPLE_SIZE = 9        
ON_TCP_PORT =5000
POLL_INTERVAL =1       # in main-loop select timeout
CONFIG_CHECK_INTERVAL =2

def parse_config(filepath):
    #cost or -1 if no link
    rows = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.replace('\t', ' ').split()
            values = [int(p) for p in parts]
            rows.append(values)

    n=len(rows)+1
    adj =[[0] * n for _ in range(n)]
    row_idx=0
    for i in range(n-1):
        row =rows[row_idx]
        row_idx += 1
        col_start =i+1
        for k,cost in enumerate(row):
            j = col_start+k
            if (j<n):
                adj[i][j] =cost
                adj[j][i] =cost  
    return n, adj


def build_link_state_msg(node_idx, vn_info, adj, num_nodes):
    """
    Build the LINK-STATE binary message for virtual node `node_idx`.
    vn_info[i] = (ip_int, udp_port) for node i (or None if not yet known).

    Each tuple: Alpha(1B) | IP(4B) | UDPPort(2B) | Cost(2B)
    The tuple for node_idx itself has Cost=0.
    """
    tuples = []
    # Self tuple (cost=0)
    ip_int, udp_port = vn_info[node_idx]
    tuples.append((node_idx, ip_int, udp_port, 0))

    # Neighbor tuples
    for j in range(num_nodes):
        if j == node_idx:
            continue
        cost = adj[node_idx][j]
        if cost > 0:   # -1 or 0 (self) excluded
            if vn_info[j] is not None:
                n_ip, n_udp = vn_info[j]
                tuples.append((j, n_ip, n_udp, cost))

    n = len(tuples)
    # Header: type(1) + N(1)
    msg = struct.pack('!BB', MSG_LINK_STATE, n)
    for (node_id, ip_int, udp_port, cost) in tuples:
        # Alpha as single byte (A=0, B=1, …)
        msg += struct.pack('!B', node_id)
        msg += struct.pack('!I', ip_int)        # 4 bytes big-endian
        msg += struct.pack('!H', udp_port)      # 2 bytes
        msg += struct.pack('!H', cost)          # 2 bytes (cost=0 means "self")
    return msg


def send_all(sock, data):
    """Send all bytes on a socket, handling partial sends."""
    total = 0
    while total < len(data):
        sent = sock.send(data[total:])
        if sent == 0:
            raise RuntimeError("socket connection broken")
        total += sent


# ─────────────────────────────────────────────
#  CONNECT message reader (from a VN)
# ─────────────────────────────────────────────
def recv_exact(sock, n):
    """Receive exactly n bytes from socket."""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def read_connect_message(sock):
    """
    Read a CONNECT message from VN.
    Format: type(1) + IP(4) + UDPport(2) = 7 bytes total.
    Returns (ip_int, udp_port) or None on error.
    """
    header = recv_exact(sock, 1)
    if header is None:
        return None
    msg_type = struct.unpack('!B', header)[0]
    if msg_type != MSG_CONNECT:
        print(f"[ON] ERROR: expected CONNECT(0x01), got 0x{msg_type:02x}")
        return None
    body = recv_exact(sock, CONNECT_BODY_LEN)
    if body is None:
        return None
    ip_int, udp_port = struct.unpack('!IH', body)
    return ip_int, udp_port


# ─────────────────────────────────────────────
#  Main Oracle Node logic
# ─────────────────────────────────────────────
def main():
    if len(sys.argv) != 2:
        print("Usage: python3 oracle_node.py <config_file>")
        sys.exit(1)

    config_path = sys.argv[1]
    if not os.path.isfile(config_path):
        print(f"[ON] ERROR: config file '{config_path}' not found")
        sys.exit(1)

    # ── Parse config ──────────────────────────
    num_nodes, adj = parse_config(config_path)
    config_mtime   = os.stat(config_path).st_mtime
    print(f"[ON] Config loaded. Network has {num_nodes} virtual nodes (A–{chr(ord('A')+num_nodes-1)})")
    for i in range(num_nodes):
        row = [f"{chr(ord('A')+j)}:{adj[i][j]}" for j in range(num_nodes) if i != j]
        print(f"[ON]   Node {chr(ord('A')+i)} neighbours: {', '.join(row) if row else 'none'}")

    # ── TCP listening socket ───────────────────
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Get own (non-loopback) IP
    own_ip = get_own_ip()
    listen_sock.bind((own_ip, ON_TCP_PORT))
    listen_sock.listen(num_nodes + 5)
    print(f"[ON] Listening for VN connections on {own_ip}:{ON_TCP_PORT}")

    # State
    vn_socks  = {}             # idx → socket
    vn_info   = [None] * num_nodes   # idx → (ip_int, udp_port)
    next_idx  = 0              # next VN index to assign
    all_connected = False
    last_config_check = time.time()

    readable_sockets = [listen_sock]

    while True:
        # ── select with timeout ────────────────
        timeout = POLL_INTERVAL
        try:
            readable, _, exceptional = select.select(readable_sockets, [], readable_sockets, timeout)
        except select.error as e:
            print(f"[ON] select error: {e}")
            break

        # ── Handle readable sockets ────────────
        for s in readable:
            if s is listen_sock:
                # New VN connecting
                conn, addr = listen_sock.accept()
                if next_idx < num_nodes:
                    idx = next_idx
                    next_idx += 1
                    vn_socks[idx] = conn
                    readable_sockets.append(conn)
                    print(f"[ON] VN {chr(ord('A')+idx)} connected from {addr}")
                else:
                    print(f"[ON] Extra connection from {addr} (disconnected island)")
                    # Keep socket but mark as island
                    conn.close()
            else:
                # Data from a VN
                idx = _sock_to_idx(s, vn_socks)
                if idx is None:
                    continue
                result = read_connect_message(s)
                if result is None:
                    print(f"[ON] VN {chr(ord('A')+idx)} disconnected or error")
                    readable_sockets.remove(s)
                    s.close()
                    del vn_socks[idx]
                    continue
                ip_int, udp_port = result
                vn_info[idx] = (ip_int, udp_port)
                ip_str = socket.inet_ntoa(struct.pack('!I', ip_int))
                print(f"[ON] CONNECT from {chr(ord('A')+idx)}: IP={ip_str}, UDP={udp_port}")

                # Check if all VNs have connected
                if all(vn_info[i] is not None for i in range(num_nodes)) and not all_connected:
                    all_connected = True
                    print("[ON] All VNs connected! Sending LINK-STATE to all...")
                    _broadcast_link_state(vn_socks, vn_info, adj, num_nodes)

        # ── Handle broken sockets ──────────────
        for s in exceptional:
            idx = _sock_to_idx(s, vn_socks)
            if idx is not None:
                print(f"[ON] Socket error for VN {chr(ord('A')+idx)}")
                readable_sockets.remove(s)
                s.close()
                del vn_socks[idx]

        # ── Periodic config file check ─────────
        now = time.time()
        if now - last_config_check >= CONFIG_CHECK_INTERVAL:
            last_config_check = now
            try:
                mtime = os.stat(config_path).st_mtime
                if mtime != config_mtime:
                    config_mtime = mtime
                    print(f"[ON] Config file changed! Reloading...")
                    num_nodes_new, adj_new = parse_config(config_path)
                    if num_nodes_new != num_nodes:
                        print(f"[ON] WARNING: node count changed ({num_nodes}→{num_nodes_new}); ignoring new count")
                    else:
                        adj = adj_new
                        print(f"[ON] New topology loaded. Re-sending LINK-STATE to all VNs...")
                        if all_connected:
                            _broadcast_link_state(vn_socks, vn_info, adj, num_nodes)
            except Exception as e:
                print(f"[ON] Config check error: {e}")

        time.sleep(0.01)  # tiny sleep to avoid busy-loop


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def get_own_ip():
    """Get the primary non-loopback IP of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        # Fallback: iterate interfaces
        import subprocess
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
        ips = result.stdout.strip().split()
        for ip in ips:
            if not ip.startswith('127.') and not ip.startswith('172.'):
                return ip
        return ips[0] if ips else '10.51.4.193'


def _sock_to_idx(sock, vn_socks):
    for idx, s in vn_socks.items():
        if s is sock:
            return idx
    return None


def _broadcast_link_state(vn_socks, vn_info, adj, num_nodes):
    for idx, sock in vn_socks.items():
        if vn_info[idx] is not None:
            msg = build_link_state_msg(idx, vn_info, adj, num_nodes)
            try:
                send_all(sock, msg)
                # Print what we sent
                tuples_info = []
                for j in range(num_nodes):
                    cost = adj[idx][j] if idx != j else 0
                    if j == idx:
                        tuples_info.append(f"{chr(ord('A')+j)}=0(self)")
                    elif cost > 0:
                        tuples_info.append(f"{chr(ord('A')+j)}={cost}")
                print(f"[ON] Sent LINK-STATE to {chr(ord('A')+idx)}: {', '.join(tuples_info)}")
            except Exception as e:
                print(f"[ON] Error sending LINK-STATE to {chr(ord('A')+idx)}: {e}")


if __name__ == '__main__':
    main()
