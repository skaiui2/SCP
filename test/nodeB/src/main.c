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
#include <sys/types.h>
#include <sys/socket.h>
#include <arpa/inet.h>

#ifndef TEST_USE_TCP
#define TEST_USE_TCP 0
#endif

#define TEST_OUTPUT_PATH    "outB.bin"
#define TEST_FILE_SIZE      (100ULL * 1024ULL * 1024ULL)
#define IO_BUF_SIZE         (64U * 1024U)

#define TEST_BIND_IP        "0.0.0.0"
#define TEST_B_PORT         6000

#define IDLE_SLEEP_US       100
#define TEST_TIMEOUT_SEC    1800
#define CLOSE_TIMEOUT_SEC   120

#if TEST_USE_TCP

#include <netinet/tcp.h>

#else

#include "cal_udp.h"
#include "scp.h"
#include "scp_time.h"

#define SCP_TEST_FD 1

struct scp_udp_user {
    cal_udp_ctx_t *udp;
    struct sockaddr_in peer;
    int peer_ready;
};

static cal_udp_ctx_t g_udp;
static struct scp_udp_user g_user;
static uint8_t g_packet_buf[2048];

static int scp_udp_send(void *user, const void *buf, size_t len)
{
    struct scp_udp_user *u = (struct scp_udp_user *)user;

    if (!u->peer_ready)
        return -1;

    return cal_udp_send(u->udp, buf, len, &u->peer);
}

static int scp_test_progress(void)
{
    int progressed = 0;

    scp_timer_process();

    /*
     * Timer callback may have completed the FIN handshake
     * and removed the stream.
     */
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

        if (!g_user.peer_ready) {
            g_user.peer = src;
            g_user.peer_ready = 1;
        }

        int ret = scp_input(NULL,
                            g_packet_buf,
                            (size_t)n);

        if (ret < 0) {
            /*
             * The previous datagram may have completed the close
             * and freed the stream. A duplicate/late datagram was
             * already queued in the UDP socket.
             */
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

        /*
         * scp_input() may have processed the final ACK and
         * freed the stream.
         */
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

static int write_all(int fd, const void *buf, size_t len)
{
    const uint8_t *p = (const uint8_t *)buf;

    while (len > 0) {
        ssize_t n = write(fd, p, len);

        if (n > 0) {
            p += (size_t)n;
            len -= (size_t)n;
            continue;
        }

        if (n < 0 && errno == EINTR)
            continue;

        return -1;
    }

    return 0;
}

#if TEST_USE_TCP

static int tcp_listen_and_accept(void)
{
    int listen_fd = -1;
    int conn_fd = -1;
    int one = 1;
    struct sockaddr_in addr;

    listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        perror("socket");
        return -1;
    }

    if (setsockopt(listen_fd,
                   SOL_SOCKET,
                   SO_REUSEADDR,
                   &one,
                   sizeof(one)) != 0) {
        perror("setsockopt(SO_REUSEADDR)");
        close(listen_fd);
        return -1;
    }

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(TEST_B_PORT);

    if (inet_pton(AF_INET, TEST_BIND_IP, &addr.sin_addr) != 1) {
        fprintf(stderr, "invalid TEST_BIND_IP: %s\n", TEST_BIND_IP);
        close(listen_fd);
        return -1;
    }

    if (bind(listen_fd,
             (struct sockaddr *)&addr,
             sizeof(addr)) != 0) {
        perror("bind");
        close(listen_fd);
        return -1;
    }

    if (listen(listen_fd, 1) != 0) {
        perror("listen");
        close(listen_fd);
        return -1;
    }

    printf("[B][TCP] listening on %s:%d\n",
           TEST_BIND_IP,
           TEST_B_PORT);

    conn_fd = accept(listen_fd, NULL, NULL);
    if (conn_fd < 0) {
        perror("accept");
        close(listen_fd);
        return -1;
    }

    close(listen_fd);
    return conn_fd;
}

#endif

int main(void)
{
    int fd_recv = -1;
    uint8_t *buf = NULL;
    uint64_t received = 0;
    int rc = 1;
    struct timespec start;
    struct timespec end;

    fd_recv = open(TEST_OUTPUT_PATH,
                   O_WRONLY | O_CREAT | O_TRUNC,
                   0644);
    if (fd_recv < 0) {
        perror("open output");
        goto out;
    }

    buf = (uint8_t *)malloc(IO_BUF_SIZE);
    if (!buf) {
        perror("malloc");
        goto out;
    }

#if TEST_USE_TCP

    int sock = tcp_listen_and_accept();
    if (sock < 0)
        goto out;

    printf("[B][TCP] connected; receiving %llu bytes into %s\n",
           (unsigned long long)TEST_FILE_SIZE,
           TEST_OUTPUT_PATH);

    if (clock_gettime(CLOCK_MONOTONIC, &start) != 0) {
        perror("clock_gettime");
        close(sock);
        goto out;
    }

    while (received < TEST_FILE_SIZE) {
        size_t want = IO_BUF_SIZE;
        uint64_t remaining = TEST_FILE_SIZE - received;

        if (remaining < want)
            want = (size_t)remaining;

        ssize_t n = recv(sock, buf, want, 0);

        if (n > 0) {
            if (write_all(fd_recv, buf, (size_t)n) != 0) {
                perror("write");
                close(sock);
                goto out;
            }

            received += (uint64_t)n;
            continue;
        }

        if (n == 0) {
            fprintf(stderr,
                    "sender closed early at %llu/%llu bytes\n",
                    (unsigned long long)received,
                    (unsigned long long)TEST_FILE_SIZE);
            close(sock);
            goto out;
        }

        if (errno == EINTR)
            continue;

        perror("recv");
        close(sock);
        goto out;
    }

    if (clock_gettime(CLOCK_MONOTONIC, &end) != 0) {
        perror("clock_gettime");
        close(sock);
        goto out;
    }

    if (shutdown(sock, SHUT_WR) != 0) {
        perror("shutdown(SHUT_WR)");
        close(sock);
        goto out;
    }

    for (;;) {
        uint8_t extra;
        ssize_t n = recv(sock, &extra, sizeof(extra), 0);

        if (n == 0)
            break;

        if (n > 0) {
            fprintf(stderr, "received data beyond the expected file size\n");
            close(sock);
            goto out;
        }

        if (errno == EINTR)
            continue;

        perror("recv waiting for sender FIN");
        close(sock);
        goto out;
    }

    close(sock);

#else

    cal_udp_open(&g_udp, TEST_BIND_IP, TEST_B_PORT);
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

    printf("[B][SCP] waiting for ESTABLISHED on %s:%d\n",
           TEST_BIND_IP,
           TEST_B_PORT);

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

    printf("[B][SCP] established; receiving %llu bytes into %s\n",
           (unsigned long long)TEST_FILE_SIZE,
           TEST_OUTPUT_PATH);

    if (clock_gettime(CLOCK_MONOTONIC, &start) != 0) {
        perror("clock_gettime");
        goto out;
    }

    deadline = monotonic_ms() + TEST_TIMEOUT_SEC * 1000ULL;

    while (received < TEST_FILE_SIZE) {
        int progressed = scp_test_progress();

        if (progressed < 0) {
            fprintf(stderr, "SCP progress failed during transfer\n");
            goto out;
        }

        for (;;) {
            size_t want = IO_BUF_SIZE;
            uint64_t remaining = TEST_FILE_SIZE - received;

            if (remaining < want)
                want = (size_t)remaining;

            if (want == 0)
                break;

            int n = scp_recv(SCP_TEST_FD, buf, want);

            if (n > 0) {
                if (write_all(fd_recv, buf, (size_t)n) != 0) {
                    perror("write");
                    goto out;
                }

                received += (uint64_t)n;
                progressed = 1;
                continue;
            }

            if (n < 0) {
                fprintf(stderr, "scp_recv failed: %d\n", n);
                goto out;
            }

            break;
        }

        if (scp_is_closed(SCP_TEST_FD)) {
            fprintf(stderr,
                    "sender closed early at %llu/%llu bytes\n",
                    (unsigned long long)received,
                    (unsigned long long)TEST_FILE_SIZE);
            goto out;
        }

        if (monotonic_ms() > deadline) {
            fprintf(stderr, "SCP transfer timeout\n");
            goto out;
        }

        if (!progressed)
            usleep(IDLE_SLEEP_US);
    }

    if (clock_gettime(CLOCK_MONOTONIC, &end) != 0) {
        perror("clock_gettime");
        goto out;
    }

    scp_close(SCP_TEST_FD);

    deadline = monotonic_ms() + CLOSE_TIMEOUT_SEC * 1000ULL;

    while (!scp_is_closed(SCP_TEST_FD)) {
        int progressed = scp_test_progress();

        if (progressed < 0) {
            fprintf(stderr, "SCP progress failed during close\n");
            goto out;
        }

        if (monotonic_ms() > deadline) {
            fprintf(stderr, "SCP close timeout\n");
            goto out;
        }

        if (!progressed)
            usleep(IDLE_SLEEP_US);
    }

#endif

    {
        double seconds = elapsed_seconds(&start, &end);
        double mbps = ((double)TEST_FILE_SIZE * 8.0) /
                      seconds /
                      1000000.0;

        printf("{\"transport\":\"%s\","
               "\"role\":\"receiver\","
               "\"bytes\":%llu,"
               "\"seconds\":%.6f,"
               "\"goodput_mbps\":%.6f,"
               "\"output\":\"%s\","
               "\"status\":\"success\"}\n",
#if TEST_USE_TCP
               "tcp",
#else
               "scp",
#endif
               (unsigned long long)received,
               seconds,
               mbps,
               TEST_OUTPUT_PATH);
    }

    rc = 0;

out:
    if (fd_recv >= 0)
        close(fd_recv);

#if !TEST_USE_TCP
    if (g_udp.sockfd >= 0)
        close(g_udp.sockfd);
#endif

    free(buf);
    return rc;
}