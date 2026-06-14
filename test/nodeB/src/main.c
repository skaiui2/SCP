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

#define MSG_DATA 1
#define MSG_DONE 2

#define RECV_BUF 2048
#define TEST_FILE_SIZE (100 * 1024 * 1024)

struct app_state {
    int local_done;
    int peer_done;
    size_t sent;
    size_t received;
};

static int send_tlv(int fd, uint8_t type, const void *data, uint16_t len)
{
    uint16_t total = 3 + len;
    uint8_t *p = malloc(total);
    if (!p) return -1;
    p[0] = type;
    p[1] = len >> 8;
    p[2] = len & 0xFF;
    if (len) memcpy(p + 3, data, len);
    int r = scp_send(fd, p, total);
    free(p);
    return r;
}

struct tlv_rx_state {
    uint8_t hdr[3];
    int hdr_have;
    uint16_t len;
    uint16_t have;
};

static void tlv_rx_init(struct tlv_rx_state *st)
{
    st->hdr_have = 0;
    st->len = 0;
    st->have = 0;
}

static int recv_tlv(int fd,
                    struct tlv_rx_state *st,
                    uint8_t *type,
                    uint8_t *buf,
                    uint16_t *len)
{
    if (st->hdr_have < 3) {
        int n = scp_recv(fd, st->hdr + st->hdr_have, 3 - st->hdr_have);
        if (n <= 0) return 0;
        st->hdr_have += n;
        if (st->hdr_have < 3) return 0;
        *type = st->hdr[0];
        st->len = ((uint16_t)st->hdr[1] << 8) | st->hdr[2];
        *len = st->len;
        st->have = 0;
    }

    if (st->have < st->len) {
        int m = scp_recv(fd, buf + st->have, st->len - st->have);
        if (m <= 0) return 0;
        st->have += m;
        if (st->have < st->len) return 0;
    }

    st->hdr_have = 0;
    return 1;
}

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
    printf("[B] full-duplex TLV test, expecting %d bytes...\n", TEST_FILE_SIZE);

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
    if (fd_send < 0) return 1;

    int fd_recv = open("outB.bin", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd_recv < 0) return 1;

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

    uint8_t sendbuf[RECV_BUF], tlvbuf[RECV_BUF], rxbuf[RECV_BUF];
    struct sockaddr_in src;

    struct app_state app = {0};
    struct tlv_rx_state tlvst; tlv_rx_init(&tlvst);

    struct scp_stream *ss = scp_stream_alloc(&st, 1, 1);

    printf("[B] waiting ESTABLISHED...\n");
    while (ss->state != SCP_ESTABLISHED) {
        scp_timer_process();
        int rn = cal_udp_recv(&udp, rxbuf, sizeof(rxbuf), &src);
        if (rn > 0) scp_input(ss, rxbuf, rn);
        usleep(1000);
    }

    printf("[B] ESTABLISHED, start full-duplex with TLV...\n");

    ssize_t cur_len = 0;
    size_t  cur_off = 0;
    int     have_pending = 0;

    while (1) {
        scp_timer_process();
        int rn = cal_udp_recv(&udp, rxbuf, sizeof(rxbuf), &src);
        if (rn > 0) scp_input(ss, rxbuf, rn);

        if (!have_pending && app.sent < TEST_FILE_SIZE) {
            cur_len = read(fd_send, sendbuf, sizeof(sendbuf));
            if (cur_len > 0) {
                cur_off = 0;
                have_pending = 1;
            }
        }

        if (have_pending) {
            uint16_t chunk = cur_len - cur_off;
            int r = send_tlv(1, MSG_DATA, sendbuf + cur_off, chunk);
            if (r == 0) {
                app.sent += chunk;
                cur_off += chunk;
                if (cur_off >= cur_len) have_pending = 0;
            } else if (r != -2) goto out;
        }

        uint8_t type; uint16_t len;
        int r = recv_tlv(1, &tlvst, &type, tlvbuf, &len);
        if (r < 0) goto out;
        if (r > 0) {
            if (type == MSG_DATA) {
                write(fd_recv, tlvbuf, len);
                app.received += len;
            } else if (type == MSG_DONE) {
                app.peer_done = 1;
                printf("[B] peer DONE received.\n");
            }
        }

        if (!app.local_done && app.received == TEST_FILE_SIZE) {
            int r2 = send_tlv(1, MSG_DONE, NULL, 0);
            if (r2 == 0) {
                app.local_done = 1;
                printf("[B] local DONE sent.\n");
            } else if (r2 != -2) goto out;
        }

        if (app.local_done && app.peer_done &&
            app.sent == TEST_FILE_SIZE &&
            app.received == TEST_FILE_SIZE) {

            printf("[B] both DONE, sending FIN...\n");
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
    printf("ALL down!!!\n");
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