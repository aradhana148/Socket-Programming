/*
 * Virtual Node (VN) - C++
 * Usage: ./virtual_node <ON_IP> <own_UDP_port> [own_IP]
 *
 * Protocol:
 *   CONNECT    (VN→ON, TCP): type(1) + IP(4) + UDPport(2)
 *   LINK-STATE (ON→VN, TCP): type(1) + N(1) + N×[Alpha(1)+IP(4)+UDPport(2)+Cost(2)]
 *   LSP        (VN↔VN, UDP): originID(1) + seqNum(4) + N_nbr(1) + N×[NbrID(1)+Cost(2)]
 */

#include <algorithm>
#include <arpa/inet.h>
#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <string>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#include <vector>

static const uint8_t MSG_CONNECT    = 0x01;
static const uint8_t MSG_LINK_STATE = 0x02;
static const int     ON_TCP_PORT    = 5000;
static const int     LSP_PERIOD_SEC = 15;
static const int     INF            = INT_MAX / 2;
static const int     MAX_NODES      = 26;
static const int     MAX_UDP_BUF    = 4096;

struct NodeInfo { bool known=false; uint32_t ip=0; uint16_t udp_port=0; };

static int       my_idx      = -1;
static uint16_t  my_udp_port = 0;
static uint32_t  my_ip_net   = 0;
static int       num_nodes   = 0;
static NodeInfo  nodes[MAX_NODES];
static int       adj[MAX_NODES][MAX_NODES];
static uint32_t  last_seq[MAX_NODES];
static bool      has_lsp[MAX_NODES];
static uint32_t  my_seq  = 0;
static int       tcp_sock = -1;
static int       udp_sock = -1;

static uint32_t get_own_ip_net() {
    struct ifaddrs *ifaddr;
    if (getifaddrs(&ifaddr) == -1) { perror("getifaddrs"); return 0; }
    uint32_t result = 0;
    for (auto *ifa = ifaddr; ifa; ifa = ifa->ifa_next) {
        if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET) continue;
        auto *sa = (struct sockaddr_in *)ifa->ifa_addr;
        uint32_t ip = ntohl(sa->sin_addr.s_addr);
        if ((ip >> 24) == 127 || (ip >> 24) == 172) continue;
        result = sa->sin_addr.s_addr;
        break;
    }
    freeifaddrs(ifaddr);
    return result;
}

static bool recv_exact(int fd, uint8_t *buf, int n) {
    for (int got = 0; got < n; ) {
        int r = recv(fd, buf + got, n - got, 0);
        if (r <= 0) return false;
        got += r;
    }
    return true;
}

static void udp_send(const uint8_t *pkt, int sz, int j) {
    if (!nodes[j].known) return;
    struct sockaddr_in dest{};
    dest.sin_family      = AF_INET;
    dest.sin_port        = htons(nodes[j].udp_port);
    dest.sin_addr.s_addr = nodes[j].ip;
    sendto(udp_sock, pkt, sz, 0, (struct sockaddr *)&dest, sizeof(dest));
    sendto(udp_sock, pkt, sz, 0, (struct sockaddr *)&dest, sizeof(dest));
}

static void send_connect() {
    uint8_t buf[7];
    buf[0] = MSG_CONNECT;
    memcpy(buf + 1, &my_ip_net, 4);
    uint16_t p = htons(my_udp_port);
    memcpy(buf + 5, &p, 2);
    for (int sent = 0; sent < 7; ) {
        int r = send(tcp_sock, buf + sent, 7 - sent, 0);
        if (r <= 0) { perror("[VN] send CONNECT"); return; }
        sent += r;
    }
    char ip[INET_ADDRSTRLEN];
    inet_ntop(AF_INET, &my_ip_net, ip, sizeof(ip));
    printf("[VN] Sent CONNECT: IP=%s UDP=%u\n", ip, my_udp_port);
}

static bool read_link_state() {
    uint8_t hdr[2];
    if (!recv_exact(tcp_sock, hdr, 2)) return false;
    if (hdr[0] != MSG_LINK_STATE) {
        printf("[VN] ERROR: expected 0x02, got 0x%02x\n", hdr[0]);
        return false;
    }
    int n = hdr[1];

    struct Tuple { uint8_t alpha; uint32_t ip_net; uint16_t udp_port; int cost; };
    std::vector<Tuple> tuples(n);
    for (int i = 0; i < n; i++) {
        uint8_t raw[9];
        if (!recv_exact(tcp_sock, raw, 9)) return false;
        tuples[i] = { raw[0], *(uint32_t*)(raw+1), ntohs(*(uint16_t*)(raw+5)),
                      (int)ntohs(*(uint16_t*)(raw+7)) };
    }

    int new_idx = -1;
    for (auto &t : tuples)
        if (t.cost == 0) { new_idx = t.alpha; my_ip_net = t.ip_net; my_udp_port = t.udp_port; break; }
    if (new_idx < 0) { printf("[VN] ERROR: no self-tuple in LINK-STATE\n"); return false; }
    my_idx = new_idx;

    for (int j = 0; j < MAX_NODES; j++) adj[my_idx][j] = adj[j][my_idx] = -1;
    adj[my_idx][my_idx] = 0;

    std::string s = std::string(1, 'A' + my_idx) + "=0";
    for (auto &t : tuples) {
        int a = t.alpha;
        if (a < MAX_NODES) { nodes[a].known = true; nodes[a].ip = t.ip_net; nodes[a].udp_port = t.udp_port; if (a >= num_nodes) num_nodes = a + 1; }
        if (t.cost > 0) { adj[my_idx][a] = adj[a][my_idx] = t.cost; s += "," + std::string(1, 'A'+a) + "=" + std::to_string(t.cost); }
    }
    printf("[VN-%c] Neighbourhood: %s\n", 'A' + my_idx, s.c_str());
    fflush(stdout);
    return true;
}

static void send_lsp(int exclude = -1) {
    if (my_idx < 0) return;
    std::vector<std::pair<int,int>> nbrs;
    for (int j = 0; j < MAX_NODES; j++)
        if (j != my_idx && adj[my_idx][j] > 0) nbrs.push_back({j, adj[my_idx][j]});

    // LSP = originID(1)+ seqNum(4)+ N_nbr(1) + N × [NbrID(1) + Cost(2)]
    int sz = 6 + (int)nbrs.size() * 3;
    std::vector<uint8_t> pkt(sz);
    pkt[0] = my_idx;
    uint32_t seq_net = htonl(++my_seq);
    memcpy(pkt.data()+1, &seq_net, 4);
    pkt[5] = (uint8_t)nbrs.size();
    int off = 6;
    for (auto &nb : nbrs) { pkt[off++] = nb.first; uint16_t c = htons(nb.second); memcpy(pkt.data()+off, &c, 2); off += 2; }

    for (auto &nb : nbrs) {
        if (nb.first == exclude) continue;
        udp_send(pkt.data(), sz, nb.first);
        char ip[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &nodes[nb.first].ip, ip, sizeof(ip));
        printf("[VN-%c] Sent LSP (seq=%u) to %c (%s:%u)\n", 'A'+my_idx, my_seq, 'A'+nb.first, ip, nodes[nb.first].udp_port);
    }
    fflush(stdout);
}

static void forward_lsp(const uint8_t *pkt, int sz, int exclude) {
    for (int j = 0; j < MAX_NODES; j++)
        if (j != my_idx && j != exclude && adj[my_idx][j] > 0) udp_send(pkt, sz, j);
}

static void run_dijkstra() {
    if (my_idx < 0 || num_nodes == 0) return;
    int dist[MAX_NODES], prev[MAX_NODES];
    bool vis[MAX_NODES];
    for (int i = 0; i < num_nodes; i++) { dist[i] = INF; vis[i] = false; prev[i] = -1; }
    dist[my_idx] = 0;

    for (int iter = 0; iter < num_nodes; iter++) {
        int u = -1;
        for (int i = 0; i < num_nodes; i++)
            if (!vis[i] && (u == -1 || dist[i] < dist[u])) u = i;
        if (u == -1 || dist[u] == INF) break;
        vis[u] = true;
        for (int v = 0; v < num_nodes; v++)
            if (adj[u][v] > 0 && dist[u] + adj[u][v] < dist[v]) { dist[v] = dist[u] + adj[u][v]; prev[v] = u; }
    }

    printf("\n[VN-%c] === Routing Table ===\n  %-8s %-12s %-10s %s\n", 'A'+my_idx, "Dest","NextHop","Cost","Path");
    for (int v = 0; v < num_nodes; v++) {
        if (v == my_idx) continue;
        if (dist[v] == INF) { printf("  %-8c %-12s %-10s\n", 'A'+v, "UNREACHABLE","INF"); continue; }
        std::vector<int> path;
        for (int x = v; x != -1; x = prev[x]) path.push_back(x);
        std::reverse(path.begin(), path.end());
        std::string ps; for (int x : path) ps += std::string(1,'A'+x) + "->";
        if (ps.size() >= 2) { ps.pop_back(); ps.pop_back(); }
        printf("  %-8c %-12c %-10d %s\n", 'A'+v, (path.size()>1?'A'+path[1]:'A'+v), dist[v], ps.c_str());
    }
    printf("[VN-%c] === Topology ===\n", 'A'+my_idx);
    for (int i = 0; i < num_nodes; i++)
        for (int j = i+1; j < num_nodes; j++)
            if (adj[i][j] > 0) printf("  %c--%c : %d\n", 'A'+i, 'A'+j, adj[i][j]);
    printf("\n"); fflush(stdout);
}

static void handle_lsp(const uint8_t *pkt, int sz, struct sockaddr_in *src) {
    if (sz < 6) return;
    int origin = pkt[0];
    uint32_t seq_net; memcpy(&seq_net, pkt+1, 4);
    uint32_t seq = ntohl(seq_net);
    int n_nbr = pkt[5];
    if (origin >= MAX_NODES || origin == my_idx) return;
    if (has_lsp[origin] && seq <= last_seq[origin]) return;
    has_lsp[origin] = true; last_seq[origin] = seq;

    printf("[VN-%c] Got LSP from %c (seq=%u, %d nbrs)\n", 'A'+my_idx, 'A'+origin, seq, n_nbr);
    for (int j = 0; j < MAX_NODES; j++) if (j != origin) adj[origin][j] = -1;
    adj[origin][origin] = 0;

    int off = 6;
    for (int i = 0; i < n_nbr && off + 2 < sz; i++) {
        int nbr = pkt[off++];
        uint16_t c_net; memcpy(&c_net, pkt+off, 2); off += 2;
        int cost = ntohs(c_net);
        if (nbr < MAX_NODES) { adj[origin][nbr] = adj[nbr][origin] = cost; }
        if (nbr >= num_nodes) num_nodes = nbr + 1;
        printf("[VN-%c]   %c--%c : %d\n", 'A'+my_idx, 'A'+origin, 'A'+nbr, cost);
    }
    if (origin >= num_nodes) num_nodes = origin + 1;

    int sender = -1;
    for (int j = 0; j < MAX_NODES; j++)
        if (nodes[j].known && nodes[j].ip == src->sin_addr.s_addr && nodes[j].udp_port == ntohs(src->sin_port))
            { sender = j; break; }
    forward_lsp(pkt, sz, sender);
    run_dijkstra();
}

int main(int argc, char *argv[]) {
    if (argc < 3) { fprintf(stderr, "Usage: %s <ON_IP> <UDP_port> [own_IP]\n", argv[0]); return 1; }

    my_udp_port = (uint16_t)atoi(argv[2]);
    my_ip_net   = (argc >= 4) ? (inet_pton(AF_INET, argv[3], &my_ip_net), my_ip_net) : get_own_ip_net();
    if ((ntohl(my_ip_net) >> 24) == 127) { fprintf(stderr, "[VN] ERROR: loopback IP\n"); return 1; }

    for (int i = 0; i < MAX_NODES; i++) {
        for (int j = 0; j < MAX_NODES; j++) adj[i][j] = (i==j) ? 0 : -1;
        has_lsp[i] = false; last_seq[i] = 0;
    }

    char own_ip[INET_ADDRSTRLEN];
    inet_ntop(AF_INET, &my_ip_net, own_ip, sizeof(own_ip));
    printf("[VN] Own IP=%s UDP=%u, connecting to ON at %s:%d\n", own_ip, my_udp_port, argv[1], ON_TCP_PORT);

    // UDP socket
    udp_sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (udp_sock < 0) { perror("[VN] UDP socket"); return 1; }
    int opt = 1; setsockopt(udp_sock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    struct sockaddr_in ua{};
    ua.sin_family = AF_INET; ua.sin_port = htons(my_udp_port); ua.sin_addr.s_addr = my_ip_net;
    if (bind(udp_sock, (struct sockaddr *)&ua, sizeof(ua)) < 0) { perror("[VN] UDP bind"); return 1; }

    // TCP socket
    tcp_sock = socket(AF_INET, SOCK_STREAM, 0);
    if (tcp_sock < 0) { perror("[VN] TCP socket"); return 1; }
    struct sockaddr_in ta{};
    ta.sin_family = AF_INET; ta.sin_port = htons(ON_TCP_PORT);
    if (inet_pton(AF_INET, argv[1], &ta.sin_addr) <= 0) { fprintf(stderr, "[VN] Bad ON IP\n"); return 1; }
    if (connect(tcp_sock, (struct sockaddr *)&ta, sizeof(ta)) < 0) { perror("[VN] TCP connect"); return 1; }
    printf("[VN] TCP connected to Oracle Node\n");

    send_connect();

    bool got_ls = false;
    while (true) {
        fd_set rfds; FD_ZERO(&rfds); FD_SET(tcp_sock, &rfds); FD_SET(udp_sock, &rfds);
        struct timeval tv{LSP_PERIOD_SEC, 0};
        int ready = select(std::max(tcp_sock, udp_sock) + 1, &rfds, NULL, NULL, &tv);
        if (ready < 0) { perror("[VN] select"); break; }

        if (ready == 0) {
            if (got_ls && my_idx >= 0) { printf("[VN-%c] Periodic LSP\n", 'A'+my_idx); send_lsp(); run_dijkstra(); }
            continue;
        }
        if (FD_ISSET(tcp_sock, &rfds)) {
            if (!read_link_state()) { printf("[VN] Oracle Node gone. Exiting.\n"); break; }
            got_ls = true;
            if (my_idx >= 0) { send_lsp(); run_dijkstra(); }
        }
        if (FD_ISSET(udp_sock, &rfds)) {
            uint8_t buf[MAX_UDP_BUF]; struct sockaddr_in src{}; socklen_t sl = sizeof(src);
            int n = recvfrom(udp_sock, buf, sizeof(buf), 0, (struct sockaddr *)&src, &sl);
            if (n > 0) handle_lsp(buf, n, &src);
        }
    }
    close(tcp_sock); close(udp_sock);
    return 0;
}
