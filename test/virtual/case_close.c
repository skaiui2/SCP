#include "sscp.h"
#include "vnet.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
extern uint32_t sscp_now_time(void);
static struct sscp_stream *sender;
static int fin_seen;
static int send_packet(void *user, const void *buf, size_t len)
{
    struct vnet_endpoint *ep = user;
    const struct sscp_hdr *hdr = buf;
    if (!ep->direction && (hdr->flags & SSCP_FLAG_FIN)) {
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
    struct sscp_transport_class ta = {.send = send_packet, .user = &ea};
    struct sscp_transport_class tb = {.send = send_packet, .user = &eb};
    struct sscp_stream *receiver;
    assert(data);
    for (i = 0; i < size; ++i) data[i] = (unsigned char)(i * 17 + i / 251);
    vnet_init(10, 100, 1000, 7);
    assert(!sscp_init(16));
    sender = sscp_stream_alloc(&ta, 1, 2); receiver = sscp_stream_alloc(&tb, 2, 1);
    assert(sender && receiver);
    assert(!sscp_set_cc(1, SSCP_CC_AIMD) && !sscp_set_cc(2, SSCP_CC_AIMD));
    assert(!sscp_connect(1));
    while (sender->state != SSCP_ESTABLISHED || receiver->state != SSCP_ESTABLISHED) {
        assert(sscp_now_time() < 13000U * SSCP_TICK_SCALE && !vnet_step());
    }
    assert(!sscp_send(1, data, size));
    assert(!sscp_send(2, data, size));
    sscp_close(1);
    assert(!fin_seen && sender->close_pending);
    while (!sscp_is_closed(1) || !sscp_is_closed(2)) {
        int n;
        assert(sscp_now_time() < 200000U * SSCP_TICK_SCALE && !vnet_step());
        if (!sscp_is_closed(1)) {
            while ((n = sscp_recv(1, buf, sizeof(buf))) > 0) {
                assert(received_a + (size_t)n <= size);
                assert(!memcmp(buf, data + received_a, (size_t)n));
                received_a += (size_t)n;
            }
        }
        if (sscp_is_closed(2)) continue;
        while ((n = sscp_recv(2, buf, sizeof(buf))) > 0) {
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
