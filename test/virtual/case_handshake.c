#include "scp.h"
#include "vnet.h"
#include "../common/transfer.h"
#include <stdio.h>
#include <stdlib.h>

extern uint32_t scp_now_time(void);
static unsigned remaining, dropped;
static struct transfer a, b;

static int send_a(void *user, const void *buf, size_t len)
{
    const struct scp_hdr *header = buf;
    if (header->flags == SCP_FLAG_ACK && remaining &&
        b.stream->state == SCP_SYN_RECV) {
        --remaining;
        ++dropped;
        return (int)len;
    }
    return vnet_send(user, buf, len);
}

int main(int argc, char **argv)
{
    struct vnet_endpoint ea = {0}, eb = {1};
    struct scp_transport_class ta = {.send = send_a, .user = &ea};
    struct scp_transport_class tb = {.send = vnet_send, .user = &eb};
    unsigned requested;
    int ok = 0, established = 0;
    if (argc != 2) return 2;
    requested = remaining = (unsigned)atoi(argv[1]);
    if (requested != 1 && requested != 2) return 2;
    vnet_init(10, 100, 0, 7);
    if (scp_init(16) ||
        transfer_init(&b, &tb, 2, 1, 0, 1048576, SCP_CC_AIMD) ||
        transfer_init(&a, &ta, 1, 2, 1, 1048576, SCP_CC_AIMD)) return 1;
    vnet_attach(&a, &b);
    while (scp_now_time() < 30000) {
        if (vnet_step() || transfer_step(&a) || transfer_step(&b)) break;
        if (!established && !scp_is_closed(a.fd) && !scp_is_closed(b.fd) &&
            a.stream->state == SCP_ESTABLISHED && b.stream->state == SCP_ESTABLISHED) {
            if (a.stream->t_hs.active || b.stream->t_hs.active) break;
            established = 1;
        }
        if (a.done && b.done) { ok = established && dropped == requested; break; }
    }
    transfer_cleanup(&a);
    transfer_cleanup(&b);
    vnet_free();
    printf("%s: dropped %u handshake ACKs; established=%d; data and close checked\n",
           ok ? "PASS" : "FAIL", dropped, established);
    return ok ? 0 : 1;
}
