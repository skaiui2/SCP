#include <stdio.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <arpa/inet.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <noise/protocol.h>
#include "cal_udp.h"
#include "scp.h"
#include "scp_time.h"

#define MSG_DATA 1
#define MSG_DONE 2

#define RECV_BUF 2048
#define NOISE_OVERHEAD 16
#define TLV_BUF (RECV_BUF + NOISE_OVERHEAD)

#define TEST_FILE_SIZE (100 * 1024 * 1024)

#pragma pack(push, 1)
struct obfs_hdr {
    uint8_t  magic[2];
    uint8_t  flags;
    uint16_t length;
};
#pragma pack(pop)

#define OBFS_MAX_PACKET 1500

struct app_state {
    int local_done;
    int peer_done;
    size_t sent;
    size_t received;
};

static inline int rand_range(int a, int b)
{
    if (b <= a) return a;
    return a + rand() % (b - a + 1);
}

static void rand_bytes(uint8_t *p, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(rand() & 0xFF);
}

static void jitter_sleep(void)
{
    int base = 1000;
    int j = rand() % 1000;
    usleep(base + j);
}

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
        if (st->len > TLV_BUF) return -1;
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
    uint8_t out[OBFS_MAX_PACKET];
    struct obfs_hdr *h = (struct obfs_hdr *)out;
    uint16_t max_payload = OBFS_MAX_PACKET - sizeof(*h) - 32 - 32;
    if (len > max_payload)
        len = max_payload;
    rand_bytes(h->magic, 2);
    h->flags = (uint8_t)(rand() & 0xFF);
    h->length = htons((uint16_t)len);
    uint8_t *p = out + sizeof(*h);
    uint8_t junk_len = (uint8_t)(rand() % 33);
    rand_bytes(p, junk_len);
    p += junk_len;
    memcpy(p, buf, len);
    p += len;
    uint8_t pad_len = (uint8_t)(rand() % 33);
    rand_bytes(p, pad_len);
    p += pad_len;
    size_t total = p - out;
    return cal_udp_send(u->udp, out, total, &u->peer);
}

static void udp_recv_and_feed_scp(cal_udp_ctx_t *udp,
                                  struct scp_stream *ss,
                                  struct sockaddr_in *src,
                                  uint8_t *rxbuf,
                                  size_t rxbuf_sz)
{
    int rn = cal_udp_recv(udp, rxbuf, rxbuf_sz, src);
    if (rn <= 0) return;
    if (rn <= (int)sizeof(struct obfs_hdr)) return;
    struct obfs_hdr *h = (struct obfs_hdr *)rxbuf;
    uint16_t plen = ntohs(h->length);
    if (plen == 0) return;
    if (plen > (uint16_t)(rn - (int)sizeof(*h))) return;
    uint8_t *payload = rxbuf + rn - plen;
    if (payload < rxbuf + sizeof(*h)) return;
    scp_input(ss, payload, plen);
}

int noise_scp_handshake_nodeB(int fd,
                              NoiseCipherState **send_cs,
                              NoiseCipherState **recv_cs,
                              cal_udp_ctx_t *udp,
                              struct scp_stream *ss,
                              struct sockaddr_in *src)
{
    NoiseHandshakeState *hs;
    NoiseCipherState *cs1, *cs2;
    NoiseProtocolId pid;
    noise_protocol_name_to_id(&pid,
        "Noise_NN_25519_ChaChaPoly_BLAKE2b",
        strlen("Noise_NN_25519_ChaChaPoly_BLAKE2b"));
    noise_handshakestate_new_by_id(&hs, &pid, NOISE_ROLE_RESPONDER);
    noise_handshakestate_start(hs);
    uint8_t in[256], out[256], rxbuf[OBFS_MAX_PACKET];
    NoiseBuffer buf;
    int n;
    for (;;) {
        scp_timer_process();
        udp_recv_and_feed_scp(udp, ss, src, rxbuf, sizeof(rxbuf));
        n = scp_recv(fd, in, sizeof(in));
        if (n > 0) break;
        jitter_sleep();
    }
    noise_buffer_set_input(buf, in, n);
    noise_handshakestate_read_message(hs, &buf, NULL);
    noise_buffer_set_output(buf, out, sizeof(out));
    noise_handshakestate_write_message(hs, &buf, NULL);
    scp_send(fd, out, buf.size);
    noise_handshakestate_split(hs, &cs1, &cs2);
    noise_handshakestate_free(hs);
    *recv_cs = cs1;
    *send_cs = cs2;
    return 0;
}

int main()
{
    srand((unsigned)time(NULL));
    printf("[B] starting full-duplex encrypted transfer...\n");
    if (access("testB.bin", F_OK) != 0) {
        int gen = open("testB.bin", O_WRONLY | O_CREAT | O_TRUNC, 0644);
        for (size_t i = 0; i < TEST_FILE_SIZE; i++) {
            uint8_t b = i % 256;
            write(gen, &b, 1);
        }
        close(gen);
    }
    int fd_send = open("testB.bin", O_RDONLY);
    int fd_recv = open("outB.bin", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    cal_udp_ctx_t udp;
    cal_udp_open(&udp, "0.0.0.0", 443);
    int fl = fcntl(udp.sockfd, F_GETFL, 0);
    fcntl(udp.sockfd, F_SETFL, fl | O_NONBLOCK);
    scp_init(16);
    scp_time_init();
    struct scp_udp_user user;
    memset(&user, 0, sizeof(user));
    user.udp = &udp;
    struct scp_transport_class st = {
        .user  = &user,
        .send  = scp_udp_send,
        .recv  = NULL,
        .close = NULL
    };
    uint8_t sendbuf[RECV_BUF], tlvbuf[TLV_BUF], rxbuf[OBFS_MAX_PACKET];
    struct sockaddr_in src;
    memset(&src, 0, sizeof(src));
    int peer_inited = 0;
    struct app_state app = {0};
    struct tlv_rx_state tlvst; tlv_rx_init(&tlvst);
    struct scp_stream *ss = scp_stream_alloc(&st, 1, 1);
    printf("[B] waiting for SCP ESTABLISHED...\n");
    while (ss->state != SCP_ESTABLISHED) {
        scp_timer_process();
        udp_recv_and_feed_scp(&udp, ss, &src, rxbuf, sizeof(rxbuf));
        if (!peer_inited && src.sin_port != 0) {
            user.peer = src;
            peer_inited = 1;
        }
        jitter_sleep();
    }
    printf("[B] SCP established, doing Noise handshake...\n");
    NoiseCipherState *send_cs, *recv_cs;
    noise_scp_handshake_nodeB(1, &send_cs, &recv_cs, &udp, ss, &src);
    printf("[B] Noise handshake OK, starting encrypted transfer...\n");
    ssize_t cur_len = 0;
    size_t  cur_off = 0;
    int     have_plain = 0;
    static uint8_t  pending_cipher[TLV_BUF];
    static uint16_t pending_cipher_len = 0;
    static uint16_t pending_plain_len  = 0;
    int             pending_valid      = 0;
    while (1) {
        scp_timer_process();
        udp_recv_and_feed_scp(&udp, ss, &src, rxbuf, sizeof(rxbuf));
        if (!peer_inited && src.sin_port != 0) {
            user.peer = src;
            peer_inited = 1;
        }
        if (!pending_valid && app.sent < TEST_FILE_SIZE) {
            if (!have_plain) {
                cur_len = read(fd_send, sendbuf, sizeof(sendbuf));
                if (cur_len > 0) {
                    cur_off = 0;
                    have_plain = 1;
                } else if (cur_len < 0) {
                    perror("[B] read");
                    goto out;
                }
            }
            if (have_plain) {
                uint16_t remain = (uint16_t)(cur_len - cur_off);
                if (remain == 0) {
                    have_plain = 0;
                } else {
                    uint16_t plain_chunk;
                    if (remain <= 128)
                        plain_chunk = remain;
                    else
                        plain_chunk = (uint16_t)rand_range(128, remain);
                    memcpy(pending_cipher, sendbuf + cur_off, plain_chunk);
                    NoiseBuffer enc;
                    noise_buffer_set_inout(enc, pending_cipher,
                                           plain_chunk, sizeof(pending_cipher));
                    int err = noise_cipherstate_encrypt(send_cs, &enc);
                    if (err != NOISE_ERROR_NONE) {
                        printf("[B] encrypt error=%d\n", err);
                        goto out;
                    }
                    pending_cipher_len = (uint16_t)enc.size;
                    pending_plain_len  = plain_chunk;
                    pending_valid      = 1;
                }
            }
        }
        if (pending_valid) {
            int r = send_tlv(1, MSG_DATA, pending_cipher, pending_cipher_len);
            if (r == 0) {
                app.sent += pending_plain_len;
                cur_off  += pending_plain_len;
                if (cur_off >= cur_len) have_plain = 0;
                pending_valid = 0;
            } else if (r != -2) {
                printf("[B] send_tlv error=%d\n", r);
                goto out;
            }
        }
        uint8_t type; uint16_t len;
        int r = recv_tlv(1, &tlvst, &type, tlvbuf, &len);
        if (r < 0) {
            printf("[B] recv_tlv error\n");
            goto out;
        }
        if (r > 0) {
            if (type == MSG_DATA) {
                uint8_t plain[TLV_BUF];
                memcpy(plain, tlvbuf, len);
                NoiseBuffer dec;
                noise_buffer_set_inout(dec, plain, len, sizeof(plain));
                int err = noise_cipherstate_decrypt(recv_cs, &dec);
                if (err != NOISE_ERROR_NONE) {
                    printf("[B] decrypt error=%d\n", err);
                    goto out;
                }
                write(fd_recv, plain, dec.size);
                app.received += dec.size;
            } else if (type == MSG_DONE) {
                app.peer_done = 1;
            }
        }
        if (!app.local_done && app.received == TEST_FILE_SIZE) {
            int r2 = send_tlv(1, MSG_DONE, NULL, 0);
            if (r2 == 0) app.local_done = 1;
            else if (r2 != -2) goto out;
        }
        if (app.local_done && app.peer_done &&
            app.sent == TEST_FILE_SIZE &&
            app.received == TEST_FILE_SIZE) {
            printf("[B] transfer complete, closing SCP...\n");
            scp_close(1);
            while (ss->state != SCP_CLOSED) {
                scp_timer_process();
                udp_recv_and_feed_scp(&udp, ss, &src, rxbuf, sizeof(rxbuf));
                jitter_sleep();
            }
            printf("[B] CLOSED.\n");
            break;
        }
        jitter_sleep();
    }
out:
    printf("[B] ALL down. sent=%zu recv=%zu\n", app.sent, app.received);
    close(fd_send);
    close(fd_recv);
    return 0;
}