#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L
#include "transfer.h"
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <inttypes.h>
#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
typedef SOCKET udp_socket;
#define UDP_INVALID INVALID_SOCKET
#define udp_close closesocket
#else
#include <unistd.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <arpa/inet.h>
typedef int udp_socket;
#define UDP_INVALID (-1)
#define udp_close close
#endif
struct udp_link {
    udp_socket socket;
    struct sockaddr_in peer;
    int peer_ready;
    uint64_t packets, wire_bytes, gap_sum;
    uint32_t first, last, min_gap, max_gap, tick_packets, max_tick;
};
extern uint32_t scp_now_time(void);
static int udp_send(void *user, const void *buf, size_t len)
{
    struct udp_link *link = user;
    const struct scp_hdr *hdr = buf;
    int n;
    uint32_t now, gap;
    if (!link->peer_ready) return -1;
    n = (int)sendto(link->socket, (const char *)buf, (int)len, 0,
                   (struct sockaddr *)&link->peer, sizeof(link->peer));
    if (n != (int)len || len < sizeof(*hdr) || !(hdr->flags & SCP_FLAG_DATA)) return n;
    now = scp_now_time();
    if (!link->packets) {
        link->first = now; link->min_gap = UINT32_MAX; link->tick_packets = 1;
    } else {
        gap = now - link->last; link->gap_sum += gap;
        if (gap < link->min_gap) link->min_gap = gap;
        if (gap > link->max_gap) link->max_gap = gap;
        link->tick_packets = gap ? 1 : link->tick_packets + 1;
    }
    if (link->tick_packets > link->max_tick) link->max_tick = link->tick_packets;
    link->last = now; ++link->packets; link->wire_bytes += len;
    return n;
}
static int udp_open(struct udp_link *link, int sender, int index, const char *peer_ip)
{
    struct sockaddr_in local = {0};
    int buffer_size = 4 * 1024 * 1024;
    link->socket = socket(AF_INET, SOCK_DGRAM, 0);
    if (link->socket == UDP_INVALID) return -1;
    local.sin_family = AF_INET;
    local.sin_port = htons((uint16_t)((sender ? 5000 : 6000) + index));
    local.sin_addr.s_addr = htonl(INADDR_ANY);
    setsockopt(link->socket, SOL_SOCKET, SO_RCVBUF, (const char *)&buffer_size, sizeof(buffer_size));
    if (bind(link->socket, (struct sockaddr *)&local, sizeof(local))) return -1;
#ifdef _WIN32
    { u_long enabled = 1; if (ioctlsocket(link->socket, FIONBIO, &enabled)) return -1; }
#else
    { int flags = fcntl(link->socket, F_GETFL, 0);
      if (flags < 0 || fcntl(link->socket, F_SETFL, flags | O_NONBLOCK)) return -1; }
#endif
    if (sender) {
        link->peer.sin_family = AF_INET;
        link->peer.sin_port = htons((uint16_t)(6000 + index));
        if (inet_pton(AF_INET, peer_ip, &link->peer.sin_addr) != 1) return -1;
        link->peer_ready = 1;
    }
    return 0;
}
static int udp_input(struct udp_link *link, struct transfer *app)
{
    for (;;) {
        unsigned char packet[2048];
        struct sockaddr_in source;
#ifdef _WIN32
        int source_len = sizeof(source);
#else
        socklen_t source_len = sizeof(source);
#endif
        int n = (int)recvfrom(link->socket, (char *)packet, sizeof(packet), 0,
                             (struct sockaddr *)&source, &source_len);
        if (n < 0) {
#ifdef _WIN32
            return WSAGetLastError() == WSAEWOULDBLOCK ? 0 : -1;
#else
            if (errno == EINTR) continue;
            return errno == EAGAIN || errno == EWOULDBLOCK ? 0 : -1;
#endif
        }
        if (!link->peer_ready) { link->peer = source; link->peer_ready = 1; }
        if (source.sin_addr.s_addr != link->peer.sin_addr.s_addr || source.sin_port != link->peer.sin_port) continue;
        if (transfer_input(app, packet, (size_t)n) < 0) return -1;
    }
}
static int fairness_step(struct transfer *app, uint64_t *count, int feed)
{
    unsigned char buf[65536];
    int n;
    if (app->started && scp_is_closed(app->fd)) return 0;
    if (app->stream->state != SCP_ESTABLISHED) return 0;
    app->established = 1;
    if (app->sender) {
        if (!feed || app->stream->snd_seq_q - app->stream->snd_una >= 4194304) return 0;
        memset(buf, app->fd, sizeof(buf));
        n = scp_send(app->fd, buf, sizeof(buf));
        if (n == SCP_ERR_NOBUF) return 0;
        if (n) return -1;
        *count += sizeof(buf);
    } else {
        while ((n = scp_recv(app->fd, buf, sizeof(buf))) > 0) *count += (uint32_t)n;
        if (n < 0) return -1;
    }
    return 0;
}
int transfer_udp_run(int sender, const char *peer_ip, uint64_t bytes,
                     int cc, int flows, uint32_t seconds, int mixed)
{
    struct udp_link links[2] = {0};
    struct transfer apps[2] = {0};
    struct scp_transport_class transports[2];
    uint64_t counts[2] = {0};
    uint32_t start, last_report, measurement = 0, round = 0, elapsed_ms = 0;
    int result = 1, initialized = 0, measuring = 0, stopping = 0, stop_sent = 0, i;
    uint64_t measured[2] = {0};
#ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2,2), &wsa)) return 1;
#endif
    for (i = 0; i < 2; ++i) links[i].socket = UDP_INVALID;
    if (scp_init(16)) goto cleanup;
    for (i = 0; i < flows; ++i) {
        if (udp_open(&links[i], sender, i, peer_ip)) goto cleanup;
        memset(&transports[i], 0, sizeof(transports[i]));
        transports[i].send = udp_send; transports[i].user = &links[i];
        ++initialized;
        if (transfer_init(&apps[i], &transports[i], i + 1, i + 1, sender, bytes,
                          mixed && i == 1 ? SCP_CC_AIMD : cc)) goto cleanup;
        if (seconds && (mixed && i == 1 ? SCP_CC_AIMD : cc) == SCP_CC_PROB &&
            scp_prob_configure(apps[i].stream, 100, 8000)) goto cleanup;
    }
    start = last_report = scp_now_time();
    while (scp_now_time() - start < (seconds ? (seconds + 120) * 1000 : 36000000)) {
        uint32_t now;
        int done = 0;
        scp_timer_process();
        for (i = 0; i < flows; ++i) {
            int index = (i + round) % flows;
            if (udp_input(&links[index], &apps[index])) goto cleanup;
        }
        now = scp_now_time();
        if (seconds) {
            int closed = 0;
            for (i = 0; i < flows; ++i)
                closed += apps[i].started && scp_is_closed(apps[i].fd);
            if (closed) {
                if (!stopping) goto cleanup;
                if (closed == flows) { result = 0; break; }
            }
            if (!measuring && !closed && apps[0].stream->state == SCP_ESTABLISHED &&
                apps[1].stream->state == SCP_ESTABLISHED) {
                if (!measurement) measurement = now;
                if (sender && now - measurement >= 1000) { measurement = now; measuring = 1; }
            }
            for (i = 0; i < flows; ++i) {
                int index = (i + round) % flows;
                if (fairness_step(&apps[index], &counts[index], measuring && !stopping)) goto cleanup;
            }
            if (!sender && !measuring && (counts[0] || counts[1])) {
                measurement = now; measuring = 1;
            }
            if (!stopping && measuring) {
                unsigned char stop;
                int n = sender ? scp_recv(apps[0].fd, &stop, 1) : 0;
                if (n < 0) goto cleanup;
                if ((sender && n == 1) || (!sender && now - measurement >= seconds * 1000)) {
                    elapsed_ms = now - measurement;
                    memcpy(measured, counts, sizeof(measured));
                    stopping = 1;
                    if (sender) for (i = 0; i < flows; ++i) scp_close(apps[i].fd);
                }
            }
            if (!sender && stopping && !stop_sent) {
                unsigned char stop = 1;
                int n = scp_send(apps[0].fd, &stop, 1);
                if (n == 0) stop_sent = 1;
                else if (n != SCP_ERR_NOBUF) goto cleanup;
            }
        } else {
            if (transfer_step(&apps[0])) goto cleanup;
            done = apps[0].done;
            if (done) { result = 0; break; }
        }
        if (now - last_report >= 1000) {
            if (seconds)
                printf("[%c][SCP] fairness bytes1=%" PRIu64 " bytes2=%" PRIu64 "\n",
                       sender ? 'A' : 'B', counts[0], counts[1]);
            else
                printf("[%c][SCP] progress sent=%" PRIu64 " received=%" PRIu64 " cwnd=%u flight=%u\n",
                       sender ? 'A' : 'B', apps[0].sent, apps[0].received,
                       apps[0].cwnd, apps[0].flight);
            last_report = now;
        }
        ++round;
#ifdef _WIN32
        Sleep(1);
#else
        usleep(100);
#endif
    }
    if (seconds && !result) {
        double elapsed = elapsed_ms / 1000.0;
        double a = (double)measured[0], b = (double)measured[1];
        if (!a || !b || !elapsed) { result = 1; goto cleanup; }
        printf("{\"type\":\"fairness\",\"role\":\"%c\",\"seconds\":%.3f,\"flow1_bytes\":%" PRIu64 ",\"flow2_bytes\":%" PRIu64 ",\"flow1_goodput_mbps\":%.6f,\"flow2_goodput_mbps\":%.6f,\"jain\":%.9f,\"status\":\"success\"}\n",
               sender ? 'A' : 'B', elapsed, measured[0], measured[1],
               a * 8 / elapsed / 1000000, b * 8 / elapsed / 1000000,
               (a + b) * (a + b) / (2 * (a * a + b * b)));
    }
    if (!seconds && !result && sender) {
        struct udp_link *s = &links[0];
        printf("{\"type\":\"pacing_observation\",\"data_datagrams\":%" PRIu64 ",\"wire_bytes\":%" PRIu64 ",\"first_ms\":%u,\"last_ms\":%u,\"min_gap_ms\":%u,\"max_gap_ms\":%u,\"avg_gap_ms\":%.6f,\"max_packets_same_tick\":%u}\n",
               s->packets, s->wire_bytes, s->first, s->last, s->packets > 1 ? s->min_gap : 0,
               s->max_gap, s->packets > 1 ? (double)s->gap_sum / (s->packets - 1) : 0, s->max_tick);
    }
cleanup:
    for (i = 0; i < initialized; ++i) transfer_cleanup(&apps[i]);
    for (i = 0; i < flows; ++i) if (links[i].socket != UDP_INVALID) udp_close(links[i].socket);
#ifdef _WIN32
    WSACleanup();
#endif
    return result;
}
