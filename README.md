# Lab 07 – Socket Programming: Virtual Network Emulator

## Overview

Two-component implementation:
- **Oracle Node (ON)** — written in **Python** (`oracle_node/oracle_node.py`)
- **Virtual Node (VN)** — written in **C++** (`virtual_node/virtual_node.cpp`)

Communication:
- ON ↔ VN: **TCP** (port 5000) for CONNECT + LINK-STATE messages (binary protocol)
- VN ↔ VN: **UDP** for Link-State Packets (LSP flooding)

---

## Build

```bash
cd virtual_node
make        # produces ./virtual_node binary
```

---

## Running the 5-node Example Network

The example config (`oracle-node-config.txt`) describes:

```
A-B=3, A-C=1
B-C=2, B-D=4
C-E=1,  D-E=7
```

**Open 6 terminals. Replace `10.51.4.193` with your machine's actual IP.**

### Terminal 1 – Oracle Node (Python)
```bash
cd oracle_node
python3 oracle_node.py ../oracle-node-config.txt
```

### Terminal 2 – VN-A (UDP port 5000)
```bash
cd virtual_node
./virtual_node 10.51.4.193 5000
```

### Terminal 3 – VN-B (UDP port 5001)
```bash
cd virtual_node
./virtual_node 10.51.4.193 5001
```

### Terminal 4 – VN-C (UDP port 5002)
```bash
cd virtual_node
./virtual_node 10.51.4.193 5002
```

### Terminal 5 – VN-D (UDP port 5003)
```bash
cd virtual_node
./virtual_node 10.51.4.193 5003
```

### Terminal 6 – VN-E (UDP port 5004)
```bash
cd virtual_node
./virtual_node 10.51.4.193 5004
```

---

## Expected Output

### Oracle Node (after all 5 VNs connect)
```
[ON] Config loaded. Network has 5 virtual nodes (A–E)
[ON] VN A connected from ('10.51.4.193', ...)
...
[ON] All VNs connected! Sending LINK-STATE to all...
[ON] Sent LINK-STATE to A: A=0(self), B=3, C=1
[ON] Sent LINK-STATE to E: E=0(self), C=1, D=7
```

### VN-E (after receiving LINK-STATE)
```
[VN-E] Neighbourhood: E=0,C=1,D=7
[VN-E] Sent LSP (seq=1) to C (10.51.4.193:5002)
[VN-E] Sent LSP (seq=1) to D (10.51.4.193:5003)

[VN-E] === Routing Table (Dijkstra) ===
  Dest     NextHop      Cost       Path
  A        C            3          E->C->A
  B        C            4          E->C->B  (or via D)
  C        C            1          E->C
  D        D            7          E->D
```

---

## Testing Topology Change

While the network is running, edit `oracle-node-config.txt` to change a link cost (e.g., change `A-B` from 3 to 10). The Oracle Node detects the change within 2 seconds, re-sends LINK-STATE to all VNs, which then re-flood LSPs and re-run Dijkstra automatically.

---

## Binary Protocol Reference

| Message | Direction | Format |
|---|---|---|
| CONNECT | VN→ON (TCP) | `type(1B=0x01)` + `IP(4B)` + `UDP_port(2B)` |
| LINK-STATE | ON→VN (TCP) | `type(1B=0x02)` + `N(1B)` + `N×[Alpha(1B)+IP(4B)+UDP(2B)+Cost(2B)]` |
| LSP | VN↔VN (UDP) | `originID(1B)` + `seqNum(4B)` + `N_nbr(1B)` + `N×[NbrID(1B)+Cost(2B)]` |

- Cost=0 in LINK-STATE tuple means "this is you"
- LSPs are sent twice per transmission for reliability
- LSPs are flooded periodically every 15 seconds and on every topology change
