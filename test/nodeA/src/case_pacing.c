#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "cal_udp.h"
#include "scp.h"
#include "scp_time.h"

#define PACING_FD 1
#define PACING_A_PORT 5100
#define PACING_B_PORT 6100
#define PACING_BYTES (8ULL * 1024ULL * 1024ULL)
#define PACING_WRITE_SIZE (64U * 1024U)
#define PACING_TIMEOUT_SEC 600U
#define PACING_IDLE_US 100U

struct pacing_tx_stats {
    uint64_t data_datagrams;
    uint64_t data_wire_bytes;
    uint64_t gap_sum_ms;
    uint32_t first_ms;
    uint32_t last_ms;
    uint32_t min_gap_ms;
    uint32_t max_gap_ms;
    uint32_t current_tick_packets;
    uint32_t max_packets_same_tick;
};

struct pacing_user {
    cal_udp_ctx_t udp;
    struct sockaddr_in peer;
    struct pacing_tx_stats stats;
};

static struct pacing_user g_pacing_user;
static uint8_t g_pacing_packet[2048];

static uint64_t monotonic_ms(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;

    return (uint64_t)ts.tv_sec * 1000ULL +
           (uint64_t)ts.tv_nsec / 1000000ULL;
}

static int set_nonblocking(int fd)
{
    int flags = fcntl(fd, F_GETFL, 0);

    if (flags < 0)
        return -1;

    return fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static void record_data_send(struct pacing_tx_stats *stats,
                             const void *buf,
                             size_t len)
{
    const struct scp_hdr *hdr;
    uint32_t now;

    if (len < sizeof(*hdr))
        return;

    hdr = (const struct scp_hdr *)buf;
    if (!(hdr->flags & SCP_FLAG_DATA))
        return;

    now = scp_now_time();

    if (stats->data_datagrams == 0) {
        stats->first_ms = now;
        stats->min_gap_ms = UINT32_MAX;
        stats->current_tick_packets = 1;
        stats->max_packets_same_tick = 1;
    } else {
        uint32_t gap = now - stats->last_ms;

        stats->gap_sum_ms += gap;
        if (gap < stats->min_gap_ms)
            stats->min_gap_ms = gap;
        if (gap > stats->max_gap_ms)
            stats->max_gap_ms = gap;

        if (now == stats->last_ms)
            stats->current_tick_packets++;
        else
            stats->current_tick_packets = 1;

        if (stats->current_tick_packets > stats->max_packets_same_tick)
            stats->max_packets_same_tick = stats->current_tick_packets;
    }

    stats->last_ms = now;
    stats->data_datagrams++;
    stats->data_wire_bytes += len;
}

static int pacing_udp_send(void *arg, const void *buf, size_t len)
{
    struct pacing_user *user = arg;

    record_data_send(&user->stats, buf, len);
    return cal_udp_send(&user->udp, buf, len, &user->peer);
}

static int pacing_progress(void)
{
    int progressed = 0;

    scp_timer_process();

    if (scp_is_closed(PACING_FD))
        return 1;

    for (;;) {
        struct sockaddr_in src;
        int n = cal_udp_recv(&g_pacing_user.udp,
                             g_pacing_packet,
                             sizeof(g_pacing_packet),
                             &src);

        if (n < 0)
            return -1;
        if (n == 0)
            break;

        if (scp_input(NULL, g_pacing_packet, (size_t)n) < 0) {
            if (scp_is_closed(PACING_FD))
                return 1;
            return -1;
        }

        progressed = 1;
        if (scp_is_closed(PACING_FD))
            break;
    }

    return progressed;
}

int test_pacing_main(const char *peer_ip)
{
    struct scp_transport_class transport;
    struct scp_stream *ss;
    uint8_t payload[PACING_WRITE_SIZE];
    uint64_t accepted = 0;
    uint64_t deadline;
    int rc = 1;
    size_t i;

    memset(&g_pacing_user, 0, sizeof(g_pacing_user));
    g_pacing_user.udp.sockfd = -1;

    for (i = 0; i < sizeof(payload); ++i)
        payload[i] = (uint8_t)i;

    if (cal_udp_open(&g_pacing_user.udp, "0.0.0.0", PACING_A_PORT) != 0)
        goto out;

    if (set_nonblocking(g_pacing_user.udp.sockfd) != 0) {
        perror("fcntl(O_NONBLOCK)");
        goto out;
    }

    g_pacing_user.peer.sin_family = AF_INET;
    g_pacing_user.peer.sin_port = htons(PACING_B_PORT);
    if (inet_pton(AF_INET, peer_ip, &g_pacing_user.peer.sin_addr) != 1) {
        fprintf(stderr, "invalid peer IP: %s\n", peer_ip);
        goto out;
    }

    srand(1);
    if (scp_init(16) != 0 || scp_time_init() != 0)
        goto out;

    memset(&transport, 0, sizeof(transport));
    transport.user = &g_pacing_user;
    transport.send = pacing_udp_send;

    ss = scp_stream_alloc(&transport, PACING_FD, PACING_FD);
    if (!ss)
        goto out;

    if (scp_set_cc(PACING_FD, SCP_CC_AIMD) != 0)
        goto out;

    if (scp_connect(PACING_FD) != 0)
        goto out;

    deadline = monotonic_ms() + PACING_TIMEOUT_SEC * 1000ULL;

    while (ss->state != SCP_ESTABLISHED) {
        int progressed = pacing_progress();

        if (progressed < 0 || scp_is_closed(PACING_FD) ||
            monotonic_ms() >= deadline)
            goto out;

        if (!progressed)
            usleep(PACING_IDLE_US);
    }

    while (accepted < PACING_BYTES) {
        size_t want = PACING_WRITE_SIZE;
        uint64_t remaining = PACING_BYTES - accepted;
        int progressed = pacing_progress();
        int ret;

        if (progressed < 0 || scp_is_closed(PACING_FD) ||
            monotonic_ms() >= deadline)
            goto out;

        if (remaining < want)
            want = (size_t)remaining;

        ret = scp_send(PACING_FD, payload, want);
        if (ret == 0) {
            accepted += want;
            progressed = 1;
        } else if (ret != SCP_ERR_NOBUF) {
            goto out;
        }

        if (!progressed)
            usleep(PACING_IDLE_US);
    }

    while (!scp_is_closed(PACING_FD)) {
        int progressed = pacing_progress();

        if (progressed < 0 || monotonic_ms() >= deadline)
            goto out;

        if (!progressed)
            usleep(PACING_IDLE_US);
    }

    {
        struct pacing_tx_stats *s = &g_pacing_user.stats;
        double avg_gap = s->data_datagrams > 1 ?
            (double)s->gap_sum_ms / (double)(s->data_datagrams - 1) : 0.0;

        printf("{\"type\":\"pacing_observation\","
               "\"clock\":\"real_scp_timerfd_ms\","
               "\"application_bytes\":%llu,"
               "\"data_datagrams_including_retransmissions\":%llu,"
               "\"data_wire_bytes_including_retransmissions\":%llu,"
               "\"first_data_send_ms\":%u,"
               "\"last_data_send_ms\":%u,"
               "\"min_gap_ms\":%u,"
               "\"max_gap_ms\":%u,"
               "\"avg_gap_ms\":%.6f,"
               "\"max_packets_same_tick\":%u}\n",
               (unsigned long long)accepted,
               (unsigned long long)s->data_datagrams,
               (unsigned long long)s->data_wire_bytes,
               s->first_ms,
               s->last_ms,
               s->data_datagrams > 1 ? s->min_gap_ms : 0,
               s->max_gap_ms,
               avg_gap,
               s->max_packets_same_tick);
    }

    rc = 0;

out:
    cal_udp_close(&g_pacing_user.udp);
    return rc;
}
