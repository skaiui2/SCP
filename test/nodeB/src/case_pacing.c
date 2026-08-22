#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L

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
#define PACING_B_PORT 6100
#define PACING_BYTES (8ULL * 1024ULL * 1024ULL)
#define PACING_READ_SIZE (64U * 1024U)
#define PACING_TIMEOUT_SEC 600U
#define PACING_CLOSE_TIMEOUT_SEC 120U
#define PACING_IDLE_US 100U

struct pacing_user {
    cal_udp_ctx_t udp;
    struct sockaddr_in peer;
    int peer_ready;
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

static int pacing_udp_send(void *arg, const void *buf, size_t len)
{
    struct pacing_user *user = arg;

    if (!user->peer_ready)
        return -1;

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

        if (!g_pacing_user.peer_ready) {
            g_pacing_user.peer = src;
            g_pacing_user.peer_ready = 1;
        }

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

int test_pacing_main(void)
{
    struct scp_transport_class transport;
    struct scp_stream *ss;
    uint8_t recv_buf[PACING_READ_SIZE];
    uint64_t received = 0;
    uint64_t deadline;
    uint64_t start_ms = 0;
    uint64_t end_ms = 0;
    int rc = 1;

    memset(&g_pacing_user, 0, sizeof(g_pacing_user));
    g_pacing_user.udp.sockfd = -1;

    if (cal_udp_open(&g_pacing_user.udp, "0.0.0.0", PACING_B_PORT) != 0)
        goto out;

    if (set_nonblocking(g_pacing_user.udp.sockfd) != 0) {
        perror("fcntl(O_NONBLOCK)");
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

    deadline = monotonic_ms() + PACING_TIMEOUT_SEC * 1000ULL;

    while (ss->state != SCP_ESTABLISHED) {
        int progressed = pacing_progress();

        if (progressed < 0 || scp_is_closed(PACING_FD) ||
            monotonic_ms() >= deadline)
            goto out;

        if (!progressed)
            usleep(PACING_IDLE_US);
    }

    start_ms = monotonic_ms();

    while (received < PACING_BYTES) {
        int progressed = pacing_progress();

        if (progressed < 0 || scp_is_closed(PACING_FD) ||
            monotonic_ms() >= deadline)
            goto out;

        for (;;) {
            size_t want = sizeof(recv_buf);
            uint64_t remaining = PACING_BYTES - received;
            int n;
            int i;

            if (remaining < want)
                want = (size_t)remaining;
            if (want == 0)
                break;

            n = scp_recv(PACING_FD, recv_buf, want);
            if (n < 0)
                goto out;
            if (n == 0)
                break;

            for (i = 0; i < n; ++i) {
                if (recv_buf[i] !=
                    (uint8_t)((received + (uint64_t)i) & 0xffU)) {
                    fprintf(stderr,
                            "pacing payload mismatch at offset %llu\n",
                            (unsigned long long)(received + (uint64_t)i));
                    goto out;
                }
            }

            received += (uint64_t)n;
            progressed = 1;
        }

        if (!progressed)
            usleep(PACING_IDLE_US);
    }

    end_ms = monotonic_ms();
    scp_close(PACING_FD);
    deadline = monotonic_ms() + PACING_CLOSE_TIMEOUT_SEC * 1000ULL;

    while (!scp_is_closed(PACING_FD)) {
        int progressed = pacing_progress();

        if (progressed < 0 || monotonic_ms() >= deadline)
            goto out;

        if (!progressed)
            usleep(PACING_IDLE_US);
    }

    printf("{\"type\":\"pacing_receiver\","
           "\"verified_bytes\":%llu,"
           "\"seconds\":%.6f}\n",
           (unsigned long long)received,
           (double)(end_ms - start_ms) / 1000.0);

    rc = 0;

out:
    cal_udp_close(&g_pacing_user.udp);
    return rc;
}
