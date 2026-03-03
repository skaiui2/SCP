#include <stdio.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <arpa/inet.h>
#include <stdlib.h>
#include <string.h>
#include "cal_udp.h"
#include "scp.h"
#include "scp_time.h"

#define RECV_BUF 2048
#define TEST_FILE_SIZE (100 * 1024 * 1024)

struct scp_udp_user {
    cal_udp_ctx_t *udp;
    struct sockaddr_in peer;
};

static int scp_udp_send(void *user, const void *buf, size_t len)
{
    struct scp_udp_user *u = user;
    return cal_udp_send(u->udp, buf, len, &u->peer);
}

int main()
{
    printf("[B] full-duplex test, expecting %d bytes...\n", TEST_FILE_SIZE);

    if (access("testB.bin", F_OK) != 0) {
        int gen = open("testB.bin", O_WRONLY | O_CREAT | O_TRUNC, 0644);
        for (size_t i = 0; i < TEST_FILE_SIZE; i++) {
            uint8_t b = i % 256;
            write(gen, &b, 1);
        }
        close(gen);
    } else {
        printf("[B] testB.bin exists, skip generating.\n");
    }

    int fd_send = open("testB.bin", O_RDONLY); 
    if (fd_send < 0) { 
        perror("open testB.bin for read"); 
        return 1; 
    } 
    
    int fd_recv = open("outB.bin", O_WRONLY | O_CREAT | O_TRUNC, 0644); 
    if (fd_recv < 0) { 
        perror("open outB.bin for write"); 
        close(fd_send); 
        return 1; 
    }

    cal_udp_ctx_t udp;
    cal_udp_open(&udp, "127.0.0.1", 6000);

    int fl = fcntl(udp.sockfd, F_GETFL, 0);
    fcntl(udp.sockfd, F_SETFL, fl | O_NONBLOCK);
    srand(time(NULL));

    scp_init(16);
    scp_time_init();

    struct scp_udp_user user;
    user.udp = &udp;
    user.peer.sin_family = AF_INET;
    user.peer.sin_port   = htons(5000);
    user.peer.sin_addr.s_addr = inet_addr("127.0.0.1");

    struct scp_transport_class st = {
        .user  = &user,
        .send  = scp_udp_send,
        .recv  = NULL,
        .close = NULL
    };

    uint8_t sendbuf[RECV_BUF];
    uint8_t recvbuf[RECV_BUF];
    uint8_t rxbuf[RECV_BUF];
    struct sockaddr_in src;

    size_t sent = 0;
    size_t received = 0;

    struct scp_stream *ss = scp_stream_alloc(&st, 1, 1);

    printf("[B] waiting ESTABLISHED...\n");
    while (ss->state != SCP_ESTABLISHED) {
        scp_timer_process();
        int rn = cal_udp_recv(&udp, rxbuf, sizeof(rxbuf), &src);
        if (rn > 0) scp_input(ss, rxbuf, rn);
        usleep(1000);
    }

    printf("[B] ESTABLISHED, start full-duplex...\n");

    ssize_t cur_len = 0;
    size_t  cur_off = 0;
    int     have_pending = 0;

    while (1) {
        scp_timer_process();

        int rn = cal_udp_recv(&udp, rxbuf, sizeof(rxbuf), &src);
        if (rn > 0) scp_input(ss, rxbuf, rn);

        if (!have_pending && sent < TEST_FILE_SIZE) {
            cur_len = read(fd_send, sendbuf, sizeof(sendbuf));
            if (cur_len > 0) {
                cur_off = 0;
                have_pending = 1;
            } else {
                have_pending = 0;
            }
        }

        if (have_pending) {
            int ret = scp_send(1, sendbuf + cur_off, (size_t)cur_len - cur_off);
            if (ret == 0) {
                sent += (size_t)cur_len;
                have_pending = 0;
            } else if (ret == -2) {

            } else {
                goto out;
            }
        }

        int n = scp_recv(1, recvbuf, sizeof(recvbuf));
        if (n > 0) {
            write(fd_recv, recvbuf, n);
            received += (size_t)n;
        }

        if (sent == TEST_FILE_SIZE &&
            ss->snd_una == ss->snd_nxt &&
            received == TEST_FILE_SIZE) {

            printf("[B] full-duplex done, sending FIN...\n");
            scp_close(1);

            while (ss->state != SCP_CLOSED) {
                scp_timer_process();
                int rn2 = cal_udp_recv(&udp, rxbuf, sizeof(rxbuf), &src);
                if (rn2 > 0) scp_input(ss, rxbuf, rn2);
                usleep(1000);
            }
            printf("[B] CLOSED.\n");
            break;
        }

        usleep(1000);
    }

out:
    close(fd_send);
    close(fd_recv);
    printf("ALL down!!!");
    return 0;
}


/*
#include <stdio.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <arpa/inet.h>
#include <stdlib.h>
#include <string.h>
#include "cal_udp.h"
#include "ikcp.h"
#include "scp_time.h"

#define RECV_BUF 2048
#define TEST_FILE_SIZE (100 * 1024 * 1024)

#define STATUS_MAGIC 0xABCDEF01

struct status_msg {
    uint32_t magic;
    uint32_t received;
};

struct kcp_udp_user {
    cal_udp_ctx_t *udp;
    struct sockaddr_in peer;
};

static int kcp_udp_output(const char *buf, int len, ikcpcb *kcp, void *user)
{
    struct kcp_udp_user *u = user;
    return cal_udp_send(u->udp, buf, len, &u->peer);
}

int main()
{
    scp_time_init();

    int gen = open("testB.bin", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    for (size_t i = 0; i < TEST_FILE_SIZE; i++) {
        uint8_t b = (i * 7) % 256;
        write(gen, &b, 1);
    }
    close(gen);

    int fd_send = open("testB.bin", O_RDONLY);
    int fd_recv = open("outB.bin", O_WRONLY | O_CREAT | O_TRUNC, 0644);

    cal_udp_ctx_t udp;
    cal_udp_open(&udp, "127.0.0.1", 6000);

    int fl = fcntl(udp.sockfd, F_GETFL, 0);
    fcntl(udp.sockfd, F_SETFL, fl | O_NONBLOCK);

    struct kcp_udp_user user;
    user.udp = &udp;
    user.peer.sin_family = AF_INET;
    user.peer.sin_port   = htons(5000);
    user.peer.sin_addr.s_addr = inet_addr("127.0.0.1");

    ikcpcb *kcp = ikcp_create(1, &user);
    ikcp_setoutput(kcp, kcp_udp_output);
    ikcp_wndsize(kcp, 1024, 1024);
    ikcp_nodelay(kcp, 1, 10, 2, 1);

    uint8_t sendbuf[RECV_BUF];
    uint8_t recvbuf[RECV_BUF];
    uint8_t rxbuf[RECV_BUF];
    struct sockaddr_in src;

    size_t sent = 0;
    size_t received = 0;
    size_t peer_received = 0;

    ssize_t cur_len = 0;
    size_t  cur_off = 0;
    int     have_pending = 0;

    uint32_t last_status_time = 0;

    while (1) {
        uint32_t now = scp_now_time();
        ikcp_update(kcp, now);

        int rn = cal_udp_recv(&udp, rxbuf, sizeof(rxbuf), &src);
        if (rn > 0) {
            ikcp_input(kcp, (const char *)rxbuf, rn);
        }

        if (!have_pending && sent < TEST_FILE_SIZE) {
            cur_len = read(fd_send, sendbuf, sizeof(sendbuf));
            if (cur_len > 0) {
                cur_off = 0;
                have_pending = 1;
            }
        }

        if (have_pending) {
            int ret = ikcp_send(kcp, (const char *)(sendbuf + cur_off),
                                (int)(cur_len - (ssize_t)cur_off));
            if (ret >= 0) {
                sent += (size_t)cur_len;
                have_pending = 0;
            }
        }

        while (1) {
            int n = ikcp_recv(kcp, (char *)recvbuf, sizeof(recvbuf));
            if (n <= 0) break;

            if (n == sizeof(struct status_msg)) {
                struct status_msg st;
                memcpy(&st, recvbuf, sizeof(st));
                if (st.magic == STATUS_MAGIC) {
                    peer_received = st.received;
                    continue;
                }
            }

            write(fd_recv, recvbuf, n);
            received += (size_t)n;
        }

        if (now - last_status_time >= 50) {
            struct status_msg st;
            st.magic = STATUS_MAGIC;
            st.received = received;
            ikcp_send(kcp, (const char *)&st, sizeof(st));
            last_status_time = now;
        }

        if (sent == TEST_FILE_SIZE &&
            received == TEST_FILE_SIZE &&
            peer_received == TEST_FILE_SIZE) {

            struct status_msg st;
            st.magic = STATUS_MAGIC;
            st.received = received;
            ikcp_send(kcp, (const char *)&st, sizeof(st));

            printf("KCP time = %u ms\n", now);
            printf("KCP data_bytes      = %llu\n", (unsigned long long)kcp->stat_data_bytes);
            printf("KCP total_bytes     = %llu\n", (unsigned long long)kcp->stat_total_bytes);
            printf("KCP retrans_bytes   = %llu\n", (unsigned long long)kcp->stat_retrans_bytes);
            if (kcp->stat_total_bytes > 0) {
                double eff = (double)kcp->stat_data_bytes / (double)kcp->stat_total_bytes;
                printf("KCP efficiency      = %.3f\n", eff);
            }
            break;
        }

        usleep(1000);
    }

    ikcp_release(kcp);
    close(fd_send);
    close(fd_recv);
    return 0;
}
*/


/*
#include <stdio.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <arpa/inet.h>
#include <stdlib.h>
#include <string.h>
#include "cal_udp.h"
#include "scp.h"

#define RECV_BUF 2048
#define TEST_FILE_SIZE (100 * 1024 * 1024)

struct scp_udp_user {
    cal_udp_ctx_t *udp;
    struct sockaddr_in peer;
};

static int scp_udp_send(void *user, const void *buf, size_t len)
{
    struct scp_udp_user *u = user;
    return cal_udp_send(u->udp, buf, len, &u->peer);
}

int main()
{
    printf("[B] one-way recv test, expecting %d bytes...\n", TEST_FILE_SIZE);

    int fd_recv = open("outB.bin", O_WRONLY | O_CREAT | O_TRUNC, 0644);

    cal_udp_ctx_t udp;
    cal_udp_open(&udp, "127.0.0.1", 6000);

    int fl = fcntl(udp.sockfd, F_GETFL, 0);
    fcntl(udp.sockfd, F_SETFL, fl | O_NONBLOCK);
    srand(time(NULL));

    scp_init(16);
    scp_time_init();

    struct scp_udp_user user;
    user.udp = &udp;
    user.peer.sin_family = AF_INET;
    user.peer.sin_port   = htons(5000);
    user.peer.sin_addr.s_addr = inet_addr("127.0.0.1");

    struct scp_transport_class st = {
        .user  = &user,
        .send  = scp_udp_send,
        .recv  = NULL,
        .close = NULL
    };

    uint8_t recvbuf[RECV_BUF];
    uint8_t rxbuf[RECV_BUF];
    struct sockaddr_in src;

    size_t received = 0;

    struct scp_stream *ss = scp_stream_alloc(&st, 1, 1);

    printf("[B] waiting ESTABLISHED...\n");
    while (ss->state != SCP_ESTABLISHED) {
        scp_timer_process();
        int rn = cal_udp_recv(&udp, rxbuf, sizeof(rxbuf), &src);
        if (rn > 0) scp_input(ss, rxbuf, rn);
        usleep(1000);
    }

    printf("[B] ESTABLISHED, start one-way recv...\n");

    while (1) {
        scp_timer_process();

        int rn = cal_udp_recv(&udp, rxbuf, sizeof(rxbuf), &src);
        if (rn > 0) scp_input(ss, rxbuf, rn);

        int n = scp_recv(1, recvbuf, sizeof(recvbuf));
        if (n > 0) {
            write(fd_recv, recvbuf, n);
            received += (size_t)n;
        }

        if (received >= TEST_FILE_SIZE &&
            ss->snd_una == ss->snd_nxt) {

            printf("[B] one-way recv done, sending FIN...\n");
            scp_close(1);

            while (ss->state != SCP_CLOSED) {
                scp_timer_process();
                int rn2 = cal_udp_recv(&udp, rxbuf, sizeof(rxbuf), &src);
                if (rn2 > 0) scp_input(ss, rxbuf, rn2);
                usleep(1000);
            }
            printf("[B] CLOSED.\n");
            break;
        }

        usleep(1000);
    }

    close(fd_recv);
    printf("ALL down!!!\n");
    return 0;
}
*/