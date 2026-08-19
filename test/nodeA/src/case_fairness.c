/* Historical test body: c94147e:test/nodeA/src/main.c; only main was renamed. */
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

#define TEST_LOCAL_IP "0.0.0.0"
#define FLOW1_A_PORT 5000
#define FLOW2_A_PORT 5001
#define FLOW1_B_PORT 6000
#define FLOW2_B_PORT 6001

#define DEFAULT_DURATION_SEC 300U
#define HANDSHAKE_TIMEOUT_SEC 60U
#define START_SETTLE_MS       1000U
#define SENDER_EXTRA_MS       3000U
#define IDLE_SLEEP_US         100U

/* Keep enough queued data to saturate the path without consuming huge RAM. */
#define APP_WRITE_SIZE  (64U * 1024U)
#define APP_QUEUE_LIMIT (4U * 1024U * 1024U)

/* Use the same PROB configuration as the current single-flow test. */
#define PROB_RTT_SPAN_MS 100U
#define PROB_LOSS_Q16    8000U

struct scp_udp_user {
    cal_udp_ctx_t *udp;
    struct sockaddr_in peer;
};

struct flow {
    int fd;
    int local_port;
    int peer_port;
    enum scp_cc_id cc;
    const char *cc_name;
    cal_udp_ctx_t udp;
    struct scp_udp_user user;
    struct scp_transport_class transport;
    struct scp_stream *ss;
    uint64_t app_enqueued;
};

static struct flow g_flow[FLOW_COUNT];
static uint8_t g_packet_buf[FLOW_COUNT][2048];
static uint8_t g_payload[FLOW_COUNT][APP_WRITE_SIZE];

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
                      int local_port,
                      int peer_port,
                      const char *peer_ip,
                      enum scp_cc_id cc)
{
    memset(f, 0, sizeof(*f));

    f->fd = fd;
    f->local_port = local_port;
    f->peer_port = peer_port;
    f->cc = cc;
    f->cc_name = cc_name(cc);
    f->udp.sockfd = -1;

    cal_udp_open(&f->udp, TEST_LOCAL_IP, local_port);
    if (f->udp.sockfd < 0) {
        fprintf(stderr,
                "cal_udp_open failed for flow fd=%d port=%d\n",
                fd,
                local_port);
        return -1;
    }

    if (set_nonblocking(f->udp.sockfd) != 0) {
        perror("fcntl(O_NONBLOCK)");
        return -1;
    }

    memset(&f->user, 0, sizeof(f->user));
    f->user.udp = &f->udp;
    f->user.peer.sin_family = AF_INET;
    f->user.peer.sin_port = htons((uint16_t)peer_port);

    if (inet_pton(AF_INET,
                  peer_ip,
                  &f->user.peer.sin_addr) != 1) {
        fprintf(stderr, "invalid peer IP: %s\n", peer_ip);
        return -1;
    }

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

static int feed_one_flow(struct flow *f,
                         const uint8_t *payload,
                         size_t payload_len)
{
    if (f->ss->snd_wmem >= APP_QUEUE_LIMIT)
        return 0;

    int ret = scp_send(f->fd, (void *)payload, payload_len);

    if (ret == 0) {
        f->app_enqueued += payload_len;
        return 1;
    }

    if (ret == SCP_ERR_NOBUF)
        return 0;

    fprintf(stderr,
            "scp_send failed for fd=%d cc=%s: %d\n",
            f->fd,
            f->cc_name,
            ret);
    return -1;
}

int test_fairness_main(int argc, char **argv)
{
    enum scp_cc_id flow1_cc;
    enum scp_cc_id flow2_cc;
    const char *mode_name;
    uint32_t duration_sec = DEFAULT_DURATION_SEC;
    int rc = 1;

    for (int i = 0; i < FLOW_COUNT; ++i)
        g_flow[i].udp.sockfd = -1;

    if (argc < 3 || argc > 4) {
        fprintf(stderr,
                "usage: %s <server-ip> prob-prob|prob-aimd [duration_sec]\n",
                argv[0]);
        return 1;
    }

    const char *peer_ip = argv[1];

    if (parse_mode(argv[2],
                   &flow1_cc,
                   &flow2_cc,
                   &mode_name) != 0) {
        fprintf(stderr, "invalid mode: %s\n", argv[2]);
        return 1;
    }

    if (argc == 4 && parse_duration(argv[3], &duration_sec) != 0) {
        fprintf(stderr, "invalid duration: %s\n", argv[3]);
        return 1;
    }

    memset(g_payload[0], 0xA5, sizeof(g_payload[0]));
    memset(g_payload[1], 0x5A, sizeof(g_payload[1]));

    srand(1);

    if (scp_init(16) != 0) {
        fprintf(stderr, "scp_init failed\n");
        goto out;
    }

    scp_time_init();

    if (setup_flow(&g_flow[0],
                   FLOW1_FD,
                   FLOW1_A_PORT,
                   FLOW1_B_PORT,
                   peer_ip,
                   flow1_cc) != 0)
        goto out;

    if (setup_flow(&g_flow[1],
                   FLOW2_FD,
                   FLOW2_A_PORT,
                   FLOW2_B_PORT,
                   peer_ip,
                   flow2_cc) != 0)
        goto out;

    if (scp_connect(FLOW1_FD) != 0 ||
        scp_connect(FLOW2_FD) != 0) {
        fprintf(stderr, "scp_connect failed\n");
        goto out;
    }

    printf("[A] peer=%s mode=%s; waiting for both streams to establish\n",
           peer_ip,
           mode_name);

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

    /* Give the receiver enough time to process both final handshake ACKs. */
    uint64_t settle_deadline = monotonic_ms() + START_SETTLE_MS;
    while (monotonic_ms() < settle_deadline) {
        int progressed = progress_all();

        if (progressed < 0)
            goto out;

        if (any_closed()) {
            fprintf(stderr, "a stream closed before the test started\n");
            goto out;
        }

        if (!progressed)
            usleep(IDLE_SLEEP_US);
    }

    printf("[A] both streams established; flow1=%s flow2=%s duration=%u s\n",
           g_flow[0].cc_name,
           g_flow[1].cc_name,
           duration_sec);

    uint64_t start_ms = monotonic_ms();
    uint64_t stop_ms = start_ms +
                       (uint64_t)duration_sec * 1000ULL +
                       SENDER_EXTRA_MS;
    uint64_t round = 0;

    while (monotonic_ms() < stop_ms) {
        int progressed = progress_all();

        if (progressed < 0)
            goto out;

        if (any_closed()) {
            fprintf(stderr, "a stream closed during the test\n");
            goto out;
        }

        /* Alternate which flow gets the first application enqueue attempt. */
        int first = (int)(round & 1ULL);
        int second = first ^ 1;

        int ret = feed_one_flow(&g_flow[first],
                                g_payload[first],
                                sizeof(g_payload[first]));
        if (ret < 0)
            goto out;
        progressed |= ret;

        ret = feed_one_flow(&g_flow[second],
                            g_payload[second],
                            sizeof(g_payload[second]));
        if (ret < 0)
            goto out;
        progressed |= ret;

        round++;

        if (!progressed)
            usleep(IDLE_SLEEP_US);
    }

    printf("{\"role\":\"sender\","
           "\"mode\":\"%s\","
           "\"duration_sec\":%u,"
           "\"flow1_cc\":\"%s\","
           "\"flow2_cc\":\"%s\","
           "\"flow1_app_enqueued\":%llu,"
           "\"flow2_app_enqueued\":%llu,"
           "\"status\":\"success\"}\n",
           mode_name,
           duration_sec,
           g_flow[0].cc_name,
           g_flow[1].cc_name,
           (unsigned long long)g_flow[0].app_enqueued,
           (unsigned long long)g_flow[1].app_enqueued);

    rc = 0;

out:
    for (int i = 0; i < FLOW_COUNT; ++i) {
        if (g_flow[i].udp.sockfd >= 0)
            close(g_flow[i].udp.sockfd);
    }

    return rc;
}
