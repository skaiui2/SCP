#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L

#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <arpa/inet.h>

#include "cal_udp.h"
#include "scp.h"
#include "scp_time.h"

#define FLOW_COUNT 2

#define FLOW1_FD 1
#define FLOW2_FD 2

#define TEST_BIND_IP "0.0.0.0"

#define FLOW1_B_PORT 6000
#define FLOW2_B_PORT 6001

#define DEFAULT_DURATION_SEC 300U
#define HANDSHAKE_TIMEOUT_SEC 60U
#define FIRST_DATA_TIMEOUT_SEC 60U
#define IDLE_SLEEP_US 100U
#define IO_BUF_SIZE   (64U * 1024U)

/* Use the same PROB configuration as the current single-flow test. */
#define PROB_RTT_SPAN_MS 100U
#define PROB_LOSS_Q16    8000U

struct scp_udp_user {
    cal_udp_ctx_t *udp;
    struct sockaddr_in peer;
    int peer_ready;
};

struct flow {
    int fd;
    int bind_port;
    enum scp_cc_id cc;
    const char *cc_name;
    cal_udp_ctx_t udp;
    struct scp_udp_user user;
    struct scp_transport_class transport;
    struct scp_stream *ss;
    uint64_t received;
};

static struct flow g_flow[FLOW_COUNT];
static uint8_t g_packet_buf[FLOW_COUNT][2048];
static uint8_t g_recv_buf[IO_BUF_SIZE];

static uint64_t monotonic_ms(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;

    return (uint64_t)ts.tv_sec * 1000ULL +
           (uint64_t)ts.tv_nsec / 1000000ULL;
}

static int scp_udp_send(void *user, const void *buf, size_t len)
{
    struct scp_udp_user *u = (struct scp_udp_user *)user;

    if (!u->peer_ready)
        return -1;

    return cal_udp_send(u->udp, buf, len, &u->peer);
}

static const char *cc_name(enum scp_cc_id cc)
{
    return cc == SCP_CC_PROB ? "prob" : "aimd";
}

static int parse_mode(const char *text,
                      enum scp_cc_id *flow1_cc,
                      enum scp_cc_id *flow2_cc,
                      const char **mode_name)
{
    if (strcmp(text, "prob-prob") == 0) {
        *flow1_cc = SCP_CC_PROB;
        *flow2_cc = SCP_CC_PROB;
        *mode_name = "prob-prob";
        return 0;
    }

    if (strcmp(text, "prob-aimd") == 0) {
        *flow1_cc = SCP_CC_PROB;
        *flow2_cc = SCP_CC_AIMD;
        *mode_name = "prob-aimd";
        return 0;
    }

    return -1;
}

static int parse_duration(const char *text, uint32_t *duration_sec)
{
    char *end = NULL;
    unsigned long value;

    errno = 0;
    value = strtoul(text, &end, 10);

    if (errno != 0 || !end || *end != '\0' || value == 0 || value > 86400UL)
        return -1;

    *duration_sec = (uint32_t)value;
    return 0;
}

static int set_nonblocking(int fd)
{
    int flags = fcntl(fd, F_GETFL, 0);

    if (flags < 0)
        return -1;

    if (fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
        return -1;

    return 0;
}

static int progress_all(void)
{
    static uint64_t poll_round = 0;
    int progressed = 0;
    int first = (int)(poll_round & 1ULL);

    poll_round++;
    scp_timer_process();

    for (int step = 0; step < FLOW_COUNT; ++step) {
        int i = first ^ step;

        for (;;) {
            struct sockaddr_in src;
            int n = cal_udp_recv(&g_flow[i].udp,
                                 g_packet_buf[i],
                                 sizeof(g_packet_buf[i]),
                                 &src);

            if (n <= 0)
                break;

            if (!g_flow[i].user.peer_ready) {
                g_flow[i].user.peer = src;
                g_flow[i].user.peer_ready = 1;
            }

            int ret = scp_input(NULL,
                                g_packet_buf[i],
                                (size_t)n);

            if (ret < 0) {
                fprintf(stderr,
                        "flow%d scp_input failed: %d\n",
                        i + 1,
                        ret);
                return -1;
            }

            progressed = 1;
        }
    }

    return progressed;
}

static int all_established(void)
{
    for (int i = 0; i < FLOW_COUNT; ++i) {
        if (!g_flow[i].ss ||
            g_flow[i].ss->state != SCP_ESTABLISHED)
            return 0;
    }

    return 1;
}

static int any_closed(void)
{
    for (int i = 0; i < FLOW_COUNT; ++i) {
        if (scp_is_closed(g_flow[i].fd))
            return 1;
    }

    return 0;
}

static int setup_flow(struct flow *f,
                      int fd,
                      int bind_port,
                      enum scp_cc_id cc)
{
    memset(f, 0, sizeof(*f));

    f->fd = fd;
    f->bind_port = bind_port;
    f->cc = cc;
    f->cc_name = cc_name(cc);
    f->udp.sockfd = -1;

    cal_udp_open(&f->udp, TEST_BIND_IP, bind_port);
    if (f->udp.sockfd < 0) {
        fprintf(stderr,
                "cal_udp_open failed for flow fd=%d port=%d\n",
                fd,
                bind_port);
        return -1;
    }

    if (set_nonblocking(f->udp.sockfd) != 0) {
        perror("fcntl(O_NONBLOCK)");
        return -1;
    }

    memset(&f->user, 0, sizeof(f->user));
    f->user.udp = &f->udp;

    f->transport.user = &f->user;
    f->transport.send = scp_udp_send;
    f->transport.recv = NULL;
    f->transport.close = NULL;

    f->ss = scp_stream_alloc(&f->transport, fd, fd);
    if (!f->ss) {
        fprintf(stderr, "scp_stream_alloc failed for fd=%d\n", fd);
        return -1;
    }

    if (scp_set_cc(fd, cc) != 0) {
        fprintf(stderr,
                "scp_set_cc failed for fd=%d cc=%s\n",
                fd,
                f->cc_name);
        return -1;
    }

    if (cc == SCP_CC_PROB) {
        if (scp_prob_configure(f->ss,
                               PROB_RTT_SPAN_MS,
                               PROB_LOSS_Q16) != 0) {
            fprintf(stderr,
                    "scp_prob_configure failed for fd=%d\n",
                    fd);
            return -1;
        }
    }

    return 0;
}

static int drain_one_flow(struct flow *f, uint64_t *added)
{
    int progressed = 0;
    uint64_t local_added = 0;

    for (;;) {
        int n = scp_recv(f->fd,
                         g_recv_buf,
                         sizeof(g_recv_buf));

        if (n > 0) {
            local_added += (uint64_t)n;
            progressed = 1;
            continue;
        }

        if (n == 0)
            break;

        fprintf(stderr,
                "scp_recv failed for fd=%d cc=%s: %d\n",
                f->fd,
                f->cc_name,
                n);
        return -1;
    }

    *added = local_added;
    return progressed;
}

int main(int argc, char **argv)
{
    enum scp_cc_id flow1_cc;
    enum scp_cc_id flow2_cc;
    const char *mode_name;
    uint32_t duration_sec = DEFAULT_DURATION_SEC;
    int rc = 1;

    for (int i = 0; i < FLOW_COUNT; ++i)
        g_flow[i].udp.sockfd = -1;

    if (argc < 2 || argc > 3) {
        fprintf(stderr,
                "usage: %s prob-prob|prob-aimd [duration_sec]\n",
                argv[0]);
        return 1;
    }

    if (parse_mode(argv[1],
                   &flow1_cc,
                   &flow2_cc,
                   &mode_name) != 0) {
        fprintf(stderr, "invalid mode: %s\n", argv[1]);
        return 1;
    }

    if (argc == 3 && parse_duration(argv[2], &duration_sec) != 0) {
        fprintf(stderr, "invalid duration: %s\n", argv[2]);
        return 1;
    }

    srand(1);

    if (scp_init(16) != 0) {
        fprintf(stderr, "scp_init failed\n");
        goto out;
    }

    scp_time_init();

    if (setup_flow(&g_flow[0],
                   FLOW1_FD,
                   FLOW1_B_PORT,
                   flow1_cc) != 0)
        goto out;

    if (setup_flow(&g_flow[1],
                   FLOW2_FD,
                   FLOW2_B_PORT,
                   flow2_cc) != 0)
        goto out;

    printf("[B] mode=%s; waiting for both streams\n", mode_name);

    uint64_t handshake_deadline =
        monotonic_ms() + (uint64_t)HANDSHAKE_TIMEOUT_SEC * 1000ULL;

    while (!all_established()) {
        int progressed = progress_all();

        if (progressed < 0)
            goto out;

        if (any_closed()) {
            fprintf(stderr, "a stream closed during handshake\n");
            goto out;
        }

        if (monotonic_ms() >= handshake_deadline) {
            fprintf(stderr, "handshake timeout\n");
            goto out;
        }

        if (!progressed)
            usleep(IDLE_SLEEP_US);
    }

    printf("[B] both streams established; flow1=%s flow2=%s; "
           "measurement starts on first payload\n",
           g_flow[0].cc_name,
           g_flow[1].cc_name);

    uint64_t first_data_deadline =
        monotonic_ms() + (uint64_t)FIRST_DATA_TIMEOUT_SEC * 1000ULL;
    uint64_t start_ms = 0;
    uint64_t end_ms = 0;
    uint64_t round = 0;

    for (;;) {
        uint64_t now = monotonic_ms();

        if (start_ms != 0 && now >= end_ms)
            break;

        int progressed = progress_all();

        if (progressed < 0)
            goto out;

        if (any_closed()) {
            fprintf(stderr, "a stream closed during the measurement\n");
            goto out;
        }

        int first = (int)(round & 1ULL);
        int second = first ^ 1;
        uint64_t added_first = 0;
        uint64_t added_second = 0;

        int ret = drain_one_flow(&g_flow[first], &added_first);
        if (ret < 0)
            goto out;
        progressed |= ret;

        ret = drain_one_flow(&g_flow[second], &added_second);
        if (ret < 0)
            goto out;
        progressed |= ret;

        if (added_first != 0 || added_second != 0) {
            if (start_ms == 0) {
                start_ms = monotonic_ms();
                end_ms = start_ms +
                         (uint64_t)duration_sec * 1000ULL;
                printf("[B] measurement started for %u seconds\n",
                       duration_sec);
            }

            g_flow[first].received += added_first;
            g_flow[second].received += added_second;
        }

        round++;

        if (start_ms == 0 && monotonic_ms() >= first_data_deadline) {
            fprintf(stderr, "first payload timeout\n");
            goto out;
        }

        if (!progressed)
            usleep(IDLE_SLEEP_US);
    }

    {
        uint64_t finish_ms = monotonic_ms();
        double seconds = (double)(finish_ms - start_ms) / 1000.0;
        double b1 = (double)g_flow[0].received;
        double b2 = (double)g_flow[1].received;
        double total = b1 + b2;
        double goodput1 = seconds > 0.0 ? b1 * 8.0 / seconds / 1000000.0 : 0.0;
        double goodput2 = seconds > 0.0 ? b2 * 8.0 / seconds / 1000000.0 : 0.0;
        double total_goodput = goodput1 + goodput2;
        double jain = 0.0;
        double flow1_share = 0.0;
        double flow1_to_flow2 = 0.0;

        if (b1 > 0.0 || b2 > 0.0) {
            double denom = 2.0 * (b1 * b1 + b2 * b2);
            if (denom > 0.0)
                jain = (total * total) / denom;

            if (total > 0.0)
                flow1_share = b1 / total;

            if (b2 > 0.0)
                flow1_to_flow2 = b1 / b2;
        }

        printf("{\"role\":\"receiver\","
               "\"mode\":\"%s\","
               "\"seconds\":%.6f,"
               "\"flow1_cc\":\"%s\","
               "\"flow2_cc\":\"%s\","
               "\"flow1_bytes\":%llu,"
               "\"flow2_bytes\":%llu,"
               "\"flow1_goodput_mbps\":%.6f,"
               "\"flow2_goodput_mbps\":%.6f,"
               "\"total_goodput_mbps\":%.6f,"
               "\"jain\":%.9f,"
               "\"flow1_share\":%.9f,"
               "\"flow1_to_flow2_ratio\":%.9f,"
               "\"status\":\"success\"}\n",
               mode_name,
               seconds,
               g_flow[0].cc_name,
               g_flow[1].cc_name,
               (unsigned long long)g_flow[0].received,
               (unsigned long long)g_flow[1].received,
               goodput1,
               goodput2,
               total_goodput,
               jain,
               flow1_share,
               flow1_to_flow2);
    }

    rc = 0;

out:
    for (int i = 0; i < FLOW_COUNT; ++i) {
        if (g_flow[i].udp.sockfd >= 0)
            close(g_flow[i].udp.sockfd);
    }

    return rc;
}