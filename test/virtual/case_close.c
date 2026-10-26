#include "scp.h"
#include "vnet.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
extern uint32_t scp_now_time(void);
static struct scp_stream *sender;
static int fin_seen;
static int send_packet(void *user, const void *buf, size_t len)
{
    struct vnet_endpoint *ep = user;
    const struct scp_hdr *hdr = buf;
    if (!ep->direction && (hdr->flags & SCP_FLAG_FIN)) {
        assert(list_empty(&sender->snd_q));
        if (!fin_seen) assert(sender->snd_una == sender->snd_seq_q);
        ++fin_seen;
    }
    return vnet_send(user, buf, len);
}
int main(void)
{
    const size_t size = 1048576;
    unsigned char *data = malloc(size), buf[65536];
    size_t received = 0, received_a = 0, i;
    struct vnet_endpoint ea = {0}, eb = {1};
    struct scp_transport_class ta = {.send = send_packet, .user = &ea};
    struct scp_transport_class tb = {.send = send_packet, .user = &eb};
    struct scp_stream *receiver;
    assert(data);
    for (i = 0; i < size; ++i) data[i] = (unsigned char)(i * 17 + i / 251);
    vnet_init(10, 100, 1000, 7);
    assert(!scp_init(16));
    sender = scp_stream_alloc(&ta, 1, 2); receiver = scp_stream_alloc(&tb, 2, 1);
    assert(sender && receiver);
    assert(!scp_set_cc(1, SCP_CC_AIMD) && !scp_set_cc(2, SCP_CC_AIMD));
    assert(!scp_connect(1));
    while (sender->state != SCP_ESTABLISHED || receiver->state != SCP_ESTABLISHED) {
        assert(scp_now_time() < 13000U * SCP_TICK_SCALE && !vnet_step());
    }
    assert(!scp_send(1, data, size));
    assert(!scp_send(2, data, size));
    scp_close(1);
    assert(!fin_seen && sender->close_pending);
    while (!scp_is_closed(1) || !scp_is_closed(2)) {
        int n;
        assert(scp_now_time() < 200000U * SCP_TICK_SCALE && !vnet_step());
        if (!scp_is_closed(1)) {
            while ((n = scp_recv(1, buf, sizeof(buf))) > 0) {
                assert(received_a + (size_t)n <= size);
                assert(!memcmp(buf, data + received_a, (size_t)n));
                received_a += (size_t)n;
            }
        }
        if (scp_is_closed(2)) continue;
        while ((n = scp_recv(2, buf, sizeof(buf))) > 0) {
            assert(received + (size_t)n <= size);
            assert(!memcmp(buf, data + received, (size_t)n));
            received += (size_t)n;
        }
    }
    assert(received == size && received_a == size && fin_seen);
    free(data); vnet_free();
    puts("PASS: close immediately after enqueue; all data acknowledged before FIN");
    return 0;
}
