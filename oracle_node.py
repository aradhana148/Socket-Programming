#!/usr/bin/env python3
# run: python3 oracle_node.py <config_file>

import sys
import os
import socket
import select
import struct
import time

MSG_CONNECT=0x01      # VN -> ON : type(1) ip(4) tcp(2)
MSG_LINK_STATE=0x02   # ON -> VN : type(1) n(1) then n x (node(1) ip(4) tcp(2) cost(2))

CONNECT_BODY_LEN=6
TUPLE_SIZE=9
ON_TCP_PORT=5000
POLL_INTERVAL=1
CONFIG_CHECK_INTERVAL=2


def parse_config(filepath):
    #upper triangle of the cost matrix
    rows=[]
    with open(filepath, 'r') as f:
        for line in f:
            line=line.strip()
            if not line or line.startswith('#'):
                continue
            parts=line.replace('\t', ' ').split()
            values=[int(p) for p in parts]
            rows.append(values)

    n=len(rows)+1
    adj=[[0]*n for _ in range(n)]
    row_idx=0
    for i in range(n-1):
        row=rows[row_idx]
        row_idx+=1
        col_start=i+1
        for k, cost in enumerate(row):
            j=col_start+k
            if j<n:
                adj[i][j]=cost
                adj[j][i]=cost 
    return n, adj


def build_link_state_msg(node_idx, vn_info, adj, num_nodes):
    tuples=[]

    ip_int, udp_port=vn_info[node_idx]
    tuples.append((node_idx, ip_int, udp_port, 0))

    for j in range(num_nodes):
        if j==node_idx:
            continue
        cost=adj[node_idx][j]
        if cost>0:
            if vn_info[j] is not None:
                n_ip, n_udp=vn_info[j]
                tuples.append((j, n_ip, n_udp, cost))

    n=len(tuples)
    msg=struct.pack('!BB', MSG_LINK_STATE, n)
    for (node_id, ip_int, udp_port, cost) in tuples:
        msg+=struct.pack('!B', node_id)    # A=0, B=1 ...
        msg+=struct.pack('!I', ip_int)
        msg+=struct.pack('!H', udp_port)
        msg+=struct.pack('!H', cost)
    return msg


def send_all(sock, data):
    total=0
    while total<len(data):
        sent=sock.send(data[total:])
        if sent==0:
            raise RuntimeError("socket connection broken")
        total+=sent


def recv_exact(sock, n):
    buf=b''
    while len(buf)<n:
        chunk=sock.recv(n-len(buf))
        if not chunk:
            return None
        buf+=chunk
    return buf


def read_connect_message(sock):

    header=recv_exact(sock, 1)
    if header is None:
        return None
    msg_type=struct.unpack('!B', header)[0]
    if msg_type!=MSG_CONNECT:
        print("got wrong msg type, expected 0x01 but got", hex(msg_type))
        return None
    body=recv_exact(sock, CONNECT_BODY_LEN)
    if body is None:
        return None
    ip_int, udp_port=struct.unpack('!IH', body)
    return ip_int, udp_port


def get_own_ip():
    try:
        s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip=s.getsockname()[0]
        s.close()
        return ip
    
    except Exception:
        import subprocess
        result=subprocess.run(['hostname', '-I'], capture_output=True, text=True)
        ips=result.stdout.strip().split()
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
            msg=build_link_state_msg(idx, vn_info, adj, num_nodes)
            try:
                send_all(sock, msg)
                tuples_info=[]
                for j in range(num_nodes):
                    cost=adj[idx][j] if idx!=j else 0
                    if j==idx:
                        tuples_info.append(f"{chr(ord('A')+j)}=0")
                    elif cost>0:
                        tuples_info.append(f"{chr(ord('A')+j)}={cost}")
                print(f"sent link state to {chr(ord('A')+idx)} ->", ",".join(tuples_info))
            except Exception as e:
                print(f"could not send link state to {chr(ord('A')+idx)}:", e)


def main():
    if len(sys.argv)!=2:
        print("usage: python3 oracle_node.py <config_file>")
        sys.exit(1)

    config_path=sys.argv[1]
    if not os.path.isfile(config_path):
        print(f"config file {config_path} not found")
        sys.exit(1)

    num_nodes, adj=parse_config(config_path)
    config_mtime=os.stat(config_path).st_mtime
    print(f"read config, {num_nodes} nodes (A to {chr(ord('A')+num_nodes-1)})")

    for i in range(num_nodes):
        row=[f"{chr(ord('A')+j)}:{adj[i][j]}" for j in range(num_nodes) if i!=j]
        print(f"  {chr(ord('A')+i)} ->", ", ".join(row) if row else "no neighbours")

    listen_sock=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    own_ip=get_own_ip()
    listen_sock.bind((own_ip, ON_TCP_PORT))
    listen_sock.listen(num_nodes+5)
    print(f"waiting for VNs on {own_ip}:{ON_TCP_PORT}")

    vn_socks={}     
    vn_info=[None]*num_nodes
    next_idx=0
    all_connected=False
    last_config_check=time.time()

    readable_sockets=[listen_sock]

    while True:
        timeout=POLL_INTERVAL
        try:
            readable, _, exceptional=select.select(readable_sockets, [], readable_sockets, timeout)
        except select.error as e:
            print("select failed:", e)
            break

        for s in readable:
            if s is listen_sock:
                conn, addr=listen_sock.accept()
                if next_idx<num_nodes:
                    
                    idx=next_idx
                    next_idx+=1
                    vn_socks[idx]=conn
                    readable_sockets.append(conn)
                    print(f"{chr(ord('A')+idx)} connected from {addr}")
                else:
                    print(f"extra connection from {addr}, closing it (island node)")
                    conn.close()
            else:
                idx=_sock_to_idx(s, vn_socks)
                if idx is None:
                    continue
                result=read_connect_message(s)
                
                if result is None:
                    print(f"{chr(ord('A')+idx)} went away")
                    readable_sockets.remove(s)
                    s.close()
                    del vn_socks[idx]
                    continue
                
                ip_int, udp_port=result
                vn_info[idx]=(ip_int, udp_port)
                ip_str=socket.inet_ntoa(struct.pack('!I', ip_int))
                print(f"CONNECT from {chr(ord('A')+idx)}: {ip_str} udp {udp_port}")

                if all(vn_info[i] is not None for i in range(num_nodes)) and not all_connected:
                    all_connected=True
                    print("everyone is here, sending link state")
                    _broadcast_link_state(vn_socks, vn_info, adj, num_nodes)

        for s in exceptional:
            idx=_sock_to_idx(s, vn_socks)
            if idx is not None:
                print(f"socket error on {chr(ord('A')+idx)}")
                readable_sockets.remove(s)
                s.close()
                del vn_socks[idx]


        now=time.time()
        if now-last_config_check>=CONFIG_CHECK_INTERVAL:
            last_config_check=now
            try:
                mtime=os.stat(config_path).st_mtime
                if mtime!=config_mtime:
                    config_mtime=mtime
                    print("config changed, reloading")
                    num_nodes_new, adj_new=parse_config(config_path)
                    if num_nodes_new!=num_nodes:
                        print(f"node count changed ({num_nodes} -> {num_nodes_new}), ignoring")
                    else:
                        adj=adj_new
                        print("new costs loaded, resending link state")
                        if all_connected:
                            _broadcast_link_state(vn_socks, vn_info, adj, num_nodes)
            except Exception as e:
                print("config check problem:", e)

        time.sleep(0.01)


if __name__=='__main__':
    main()