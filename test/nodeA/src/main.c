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
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <arpa/inet.h>

#define TEST_SCP_CC 0

#ifndef TEST_USE_TCP
#define TEST_USE_TCP 0
#endif

#define TEST_FILE_PATH      "testA.bin"
#define TEST_FILE_SIZE      (100ULL * 1024ULL * 1024ULL)
#define IO_BUF_SIZE         (64U * 1024U)

#define TEST_LOCAL_IP       "0.0.0.0"
#define TEST_PEER_IP        "10.0.2.1"
#define TEST_A_PORT         5000
#define TEST_B_PORT         6000

#define IDLE_SLEEP_US       100
#define TEST_TIMEOUT_SEC    4000

#if TEST_USE_TCP

#include <netinet/tcp.h>

#ifndef TCP_CC_NAME
#define TCP_CC_NAME "cubic"
#endif

#else

#include "cal_udp.h"
#include "scp.h"
#include "scp_time.h"

#define SCP_TEST_FD 1

struct scp_udp_user {
    cal_udp_ctx_t *udp;
    struct sockaddr_in peer;
};

static cal_udp_ctx_t g_udp;
static struct scp_udp_user g_user;
static uint8_t g_packet_buf[2048];

static int scp_udp_send(void *user, const void *buf, size_t len)
{
    struct scp_udp_user *u = (struct scp_udp_user *)user;
    return cal_udp_send(u->udp, buf, len, &u->peer);
}

static int scp_test_progress(void)
{
    int progressed = 0;

    scp_timer_process();

    if (scp_is_closed(SCP_TEST_FD))
        return 1;

    for (;;) {
        struct sockaddr_in src;

        int n = cal_udp_recv(&g_udp,
                             g_packet_buf,
                             sizeof(g_packet_buf),
                             &src);

        if (n <= 0)
            break;

        int ret = scp_input(NULL,
                            g_packet_buf,
                            (size_t)n);

        if (ret < 0) {
            if (scp_is_closed(SCP_TEST_FD)) {
                progressed = 1;
                break;
            }

            fprintf(stderr,
                    "scp_input failed before stream close: %d\n",
                    ret);
            return -1;
        }

        progressed = 1;

        if (scp_is_closed(SCP_TEST_FD))
            break;
    }

    return progressed;
}

#endif

#if !TEST_USE_TCP
static uint64_t monotonic_ms(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;

    return (uint64_t)ts.tv_sec * 1000ULL +
           (uint64_t)ts.tv_nsec / 1000000ULL;
}
#endif

static double elapsed_seconds(const struct timespec *start,
                              const struct timespec *end)
{
    return (double)(end->tv_sec - start->tv_sec) +
           (double)(end->tv_nsec - start->tv_nsec) / 1000000000.0;
}

static int verify_input_file(int fd)
{
    struct stat st;

    if (fstat(fd, &st) != 0) {
        perror("fstat");
        return -1;
    }

    if ((uint64_t)st.st_size != TEST_FILE_SIZE) {
        fprintf(stderr,
                "input file must be exactly %llu bytes; actual=%lld\n",
                (unsigned long long)TEST_FILE_SIZE,
                (long long)st.st_size);
        return -1;
    }

    return 0;
}

#if TEST_USE_TCP

static int tcp_connect_to_receiver(void)
{
    int fd = -1;
    int one = 1;
    struct sockaddr_in peer;

    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        perror("socket");
        return -1;
    }

    if (setsockopt(fd,
                   IPPROTO_TCP,
                   TCP_CONGESTION,
                   TCP_CC_NAME,
                   strlen(TCP_CC_NAME)) != 0) {
        fprintf(stderr,
                "warning: cannot select TCP congestion control '%s': %s; "
                "using the system default\n",
                TCP_CC_NAME,
                strerror(errno));
    }

    if (setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one)) != 0) {
        perror("setsockopt(SO_KEEPALIVE)");
        close(fd);
        return -1;
    }

    memset(&peer, 0, sizeof(peer));
    peer.sin_family = AF_INET;
    peer.sin_port = htons(TEST_B_PORT);

    if (inet_pton(AF_INET, TEST_PEER_IP, &peer.sin_addr) != 1) {
        fprintf(stderr, "invalid TEST_PEER_IP: %s\n", TEST_PEER_IP);
        close(fd);
        return -1;
    }

    if (connect(fd, (struct sockaddr *)&peer, sizeof(peer)) != 0) {
        perror("connect");
        close(fd);
        return -1;
    }

    return fd;
}

#endif

int main(void)
{
    int fd_send = -1;
    uint8_t *buf = NULL;
    uint64_t accepted = 0;
    int rc = 1;
    struct timespec start;
    struct timespec end;

    fd_send = open(TEST_FILE_PATH, O_RDONLY);
    if (fd_send < 0) {
        perror("open input");
        goto out;
    }

    if (verify_input_file(fd_send) != 0)
        goto out;

    buf = (uint8_t *)malloc(IO_BUF_SIZE);
    if (!buf) {
        perror("malloc");
        goto out;
    }

#if TEST_USE_TCP

    int sock = tcp_connect_to_receiver();
    if (sock < 0)
        goto out;

    printf("[A][TCP/%s] connected; sending %llu bytes from %s\n",
           TCP_CC_NAME,
           (unsigned long long)TEST_FILE_SIZE,
           TEST_FILE_PATH);

    if (clock_gettime(CLOCK_MONOTONIC, &start) != 0) {
        perror("clock_gettime");
        close(sock);
        goto out;
    }

    while (accepted < TEST_FILE_SIZE) {
        size_t want = IO_BUF_SIZE;
        uint64_t remaining = TEST_FILE_SIZE - accepted;

        if (remaining < want)
            want = (size_t)remaining;

        ssize_t nr;
        do {
            nr = read(fd_send, buf, want);
        } while (nr < 0 && errno == EINTR);

        if (nr < 0) {
            perror("read");
            close(sock);
            goto out;
        }

        if (nr == 0) {
            fprintf(stderr, "unexpected EOF in input file\n");
            close(sock);
            goto out;
        }

        size_t off = 0;
        while (off < (size_t)nr) {
            ssize_t nw = send(sock,
                              buf + off,
                              (size_t)nr - off,
                              MSG_NOSIGNAL);

            if (nw > 0) {
                off += (size_t)nw;
                accepted += (uint64_t)nw;
                continue;
            }

            if (nw < 0 && errno == EINTR)
                continue;

            perror("send");
            close(sock);
            goto out;
        }
    }

    for (;;) {
        uint8_t dummy;
        ssize_t n = recv(sock, &dummy, sizeof(dummy), 0);

        if (n == 0)
            break;

        if (n > 0) {
            fprintf(stderr, "unexpected payload from receiver\n");
            close(sock);
            goto out;
        }

        if (errno == EINTR)
            continue;

        perror("recv waiting for receiver FIN");
        close(sock);
        goto out;
    }

    if (clock_gettime(CLOCK_MONOTONIC, &end) != 0) {
        perror("clock_gettime");
        close(sock);
        goto out;
    }

    if (shutdown(sock, SHUT_WR) != 0 && errno != ENOTCONN) {
        perror("shutdown(SHUT_WR)");
        close(sock);
        goto out;
    }

    close(sock);

#else

    cal_udp_open(&g_udp, TEST_LOCAL_IP, TEST_A_PORT);
    if (g_udp.sockfd < 0) {
        fprintf(stderr, "cal_udp_open failed\n");
        goto out;
    }

    int flags = fcntl(g_udp.sockfd, F_GETFL, 0);
    if (flags < 0 ||
        fcntl(g_udp.sockfd, F_SETFL, flags | O_NONBLOCK) < 0) {
        perror("fcntl(O_NONBLOCK)");
        goto out;
    }

    srand(1);

    if (scp_init(16) != 0) {
        fprintf(stderr, "scp_init failed\n");
        goto out;
    }

    scp_time_init();

    memset(&g_user, 0, sizeof(g_user));
    g_user.udp = &g_udp;
    g_user.peer.sin_family = AF_INET;
    g_user.peer.sin_port = htons(TEST_B_PORT);

    if (inet_pton(AF_INET,
                  TEST_PEER_IP,
                  &g_user.peer.sin_addr) != 1) {
        fprintf(stderr, "invalid TEST_PEER_IP: %s\n", TEST_PEER_IP);
        goto out;
    }

    struct scp_transport_class transport = {
        .user = &g_user,
        .send = scp_udp_send,
        .recv = NULL,
        .close = NULL
    };

    struct scp_stream *ss =
        scp_stream_alloc(&transport, SCP_TEST_FD, SCP_TEST_FD);

    if (!ss) {
        fprintf(stderr, "scp_stream_alloc failed\n");
        goto out;
    }

    if (scp_set_cc(SCP_TEST_FD,
                   TEST_SCP_CC) != 0) {
        fprintf(stderr,
                "scp_set_cc failed: %d\n",
                TEST_SCP_CC);
        goto out;
    }

    scp_prob_configure(ss, 100, 8000);

    if (scp_connect(SCP_TEST_FD) != 0) {
        fprintf(stderr, "scp_connect failed\n");
        goto out;
    }

    printf("[A][SCP] waiting for ESTABLISHED\n");

    uint64_t deadline = monotonic_ms() + TEST_TIMEOUT_SEC * 1000ULL;

    while (ss->state != SCP_ESTABLISHED) {
        int progressed = scp_test_progress();

        if (progressed < 0) {
            fprintf(stderr, "SCP progress failed during handshake\n");
            goto out;
        }

        if (scp_is_closed(SCP_TEST_FD)) {
            fprintf(stderr, "SCP closed during handshake\n");
            goto out;
        }

        if (monotonic_ms() > deadline) {
            fprintf(stderr, "SCP handshake timeout\n");
            goto out;
        }

        if (!progressed)
            usleep(IDLE_SLEEP_US);
    }

    printf("[A][SCP] established; sending %llu bytes from %s\n",
           (unsigned long long)TEST_FILE_SIZE,
           TEST_FILE_PATH);

    if (clock_gettime(CLOCK_MONOTONIC, &start) != 0) {
        perror("clock_gettime");
        goto out;
    }

    size_t pending_len = 0;

    deadline = monotonic_ms() + TEST_TIMEOUT_SEC * 1000ULL;

    while (accepted < TEST_FILE_SIZE) {
        int progressed = scp_test_progress();

        if (progressed < 0) {
            fprintf(stderr, "SCP progress failed during transfer\n");
            goto out;
        }

        if (pending_len == 0) {
            size_t want = IO_BUF_SIZE;
            uint64_t remaining = TEST_FILE_SIZE - accepted;

            if (remaining < want)
                want = (size_t)remaining;

            ssize_t nr;
            do {
                nr = read(fd_send, buf, want);
            } while (nr < 0 && errno == EINTR);

            if (nr < 0) {
                perror("read");
                goto out;
            }

            if (nr == 0) {
                fprintf(stderr, "unexpected EOF in input file\n");
                goto out;
            }

            pending_len = (size_t)nr;
        }

        int ret = scp_send(SCP_TEST_FD, buf, pending_len);

        if (ret == 0) {
            accepted += (uint64_t)pending_len;
            pending_len = 0;
            progressed = 1;
        } else if (ret == SCP_ERR_NOBUF) {
        } else {
            fprintf(stderr, "scp_send failed: %d\n", ret);
            goto out;
        }

        if (scp_is_closed(SCP_TEST_FD)) {
            fprintf(stderr, "receiver closed before all bytes were accepted\n");
            goto out;
        }

        if (monotonic_ms() > deadline) {
            fprintf(stderr, "SCP transfer timeout\n");
            goto out;
        }

        if (!progressed)
            usleep(IDLE_SLEEP_US);
    }

    while (!scp_is_closed(SCP_TEST_FD)) {
        int progressed = scp_test_progress();

        if (progressed < 0) {
            fprintf(stderr, "SCP progress failed while waiting for close\n");
            goto out;
        }

        if (monotonic_ms() > deadline) {
            fprintf(stderr, "SCP close wait timeout\n");
            goto out;
        }

        if (!progressed)
            usleep(IDLE_SLEEP_US);
    }

    if (clock_gettime(CLOCK_MONOTONIC, &end) != 0) {
        perror("clock_gettime");
        goto out;
    }

#endif

    {
        double seconds = elapsed_seconds(&start, &end);
        double mbps = ((double)TEST_FILE_SIZE * 8.0) /
                      seconds /
                      1000000.0;

        printf("{\"transport\":\"%s\","
               "\"role\":\"sender\","
               "\"bytes\":%llu,"
               "\"seconds_including_receiver_close\":%.6f,"
               "\"goodput_mbps\":%.6f,"
               "\"status\":\"success\"}\n",
#if TEST_USE_TCP
               "tcp",
#else
               "scp",
#endif
               (unsigned long long)accepted,
               seconds,
               mbps);
    }

    rc = 0;

out:
    if (fd_send >= 0)
        close(fd_send);

#if !TEST_USE_TCP
    if (g_udp.sockfd >= 0)
        close(g_udp.sockfd);
#endif

    free(buf);
    return rc;
}