#include <assert.h>
#include "../../scp.c"
#include "vnet.h"
#include "../common/transfer.h"

static unsigned arrivals, inputs, sent_count, ack_count;
static uint64_t delivered;
static uint32_t sequence[64], sent_at[64], size[64];
static unsigned char confirmed[64];
static struct transfer transfer_a, transfer_b;

static void observe_network(unsigned direction, int event, const void *buf, size_t len, uint32_t now)
{
    const struct scp_hdr *hdr = buf;
    (void)direction;
    if (event == VNET_INPUT_RETURNED) ++inputs;
    if (event != VNET_ARRIVAL) return;
    assert(len == 1000 && ((const unsigned char *)buf)[sizeof(*hdr)] == 0x5a);
    ++arrivals;
    delivered |= UINT64_C(1) << ntohl(hdr->seq);
    if (now < 11) assert(now == 3);
}

static void raw_packet(unsigned direction, unsigned seq)
{
    struct vnet_endpoint ep = {direction};
    unsigned char wire[1000];
    struct scp_hdr hdr = {0};
    memset(wire, 0x5a, sizeof(wire));
    hdr.fd = 15;
    hdr.flags = SCP_FLAG_ACK;
    hdr.seq = htonl(seq);
    hdr.len = htons(sizeof(wire) - sizeof(hdr));
    memcpy(wire, &hdr, sizeof(hdr));
    assert(vnet_send(&ep, wire, sizeof(wire)) == sizeof(wire));
    memset(wire, 0, sizeof(wire));
}

static uint64_t network_batch(uint32_t rate, uint32_t loss, uint32_t seed)
{
    unsigned i;
    vnet_init(3, rate, loss, seed);
    vnet_observe(observe_network);
    arrivals = inputs = 0; delivered = 0;
    for (i = 0; i < 64; ++i) raw_packet(i % 2, i);
    for (i = 1; i <= 300; ++i) {
        assert(!vnet_step() && scp_now_time() == i);
        if (!rate && i < 3) assert(!arrivals);
        if (!rate && i == 3) assert(arrivals + vnet_dropped == 64);
        if (rate && i < 11) assert(!arrivals);
        if (rate && i == 11) assert(arrivals == 2);
        if (rate && i == 19) assert(arrivals == 4);
    }
    assert(vnet_packets == 64 && arrivals + vnet_dropped == 64 && inputs == arrivals);
    if (!loss) assert(delivered == UINT64_MAX);
    if (loss == 1000000) assert(!delivered && !arrivals);
    vnet_free();
    return delivered;
}

static void after_delivery(void *arg)
{
    assert(arrivals == 1 && inputs == 1);
    ++*(unsigned *)arg;
}

static void observe_transfer(unsigned direction, int event, const void *buf, size_t len, uint32_t now)
{
    const struct scp_hdr *hdr = buf;
    unsigned i;
    uint32_t bytes = ntohs(hdr->len);
    if (!direction && event == VNET_SEND && hdr->flags == SCP_FLAG_DATA) {
        assert(sent_count < 33 && len == sizeof(*hdr) + bytes);
        assert(bytes == (sent_count == 32 ? 17 : MSS));
        sequence[sent_count] = ntohl(hdr->seq);
        sent_at[sent_count] = now;
        size[sent_count++] = bytes;
    }
    if (direction != 1 || event != VNET_INPUT_RETURNED || hdr->flags != SCP_FLAG_ACK) return;
    for (i = 0; i < sent_count; ++i) {
        if (ntohl(hdr->ack) != sequence[i] + size[i]) continue;
        assert(!confirmed[i] && now - sent_at[i] == 100);
        assert(ntohl(hdr->time_val) == sent_at[i]);
        confirmed[i] = 1;
        ++ack_count;
        assert(!scp_is_closed(transfer_a.fd));
        assert(transfer_a.stream->cwnd == MSS + (i < 32 ? (i + 1) * MSS : 32 * MSS + 17));
        assert(transfer_a.stream->srtt == 100);
        break;
    }
}

static void native_transfer(uint32_t backlog, int observe)
{
    struct vnet_endpoint ea = {0}, eb = {1};
    struct scp_transport_class ta = {.send = vnet_send, .user = &ea};
    struct scp_transport_class tb = {.send = vnet_send, .user = &eb};
    uint64_t total = 32 * MSS + 17;
    int ok = 0;
    vnet_init(50, 0, 0, 7);
    if (observe) vnet_observe(observe_transfer);
    sent_count = ack_count = 0;
    memset(confirmed, 0, sizeof(confirmed));
    assert(!transfer_init(&transfer_b, &tb, 2, 1, 0, total, SCP_CC_AIMD));
    assert(!transfer_init(&transfer_a, &ta, 1, 2, 1, total, SCP_CC_AIMD));
    transfer_a.send_backlog_bytes = backlog;
    vnet_attach(&transfer_a, &transfer_b);
    while (scp_now_time() < 10000) {
        assert(!vnet_step() && !transfer_step(&transfer_a) && !transfer_step(&transfer_b));
        if (!transfer_a.done) assert(transfer_a.stream->snd_seq_q - transfer_a.stream->snd_una <= backlog);
        if (transfer_a.done && transfer_b.done) { ok = 1; break; }
    }
    assert(ok && transfer_a.sent == total && transfer_b.received == total && transfer_a.hash == transfer_b.hash);
    assert(!vnet_dropped && (observe ? sent_count == 33 && ack_count == 33 : !sent_count));
    transfer_cleanup(&transfer_a); transfer_cleanup(&transfer_b);
    assert(!scp_timer_tree.count && list_empty(&scp_stream_queue));
    vnet_free();
}

int main(void)
{
    uint64_t pattern;
    unsigned fired = 0;
    struct scp_timer timer = {0};
    assert(!scp_init(16));
    network_batch(0, 0, 7);
    network_batch(1, 0, 7);
    network_batch(0, 1000000, 7);
    pattern = network_batch(0, 500000, 7);
    assert(pattern && pattern != UINT64_MAX && pattern == network_batch(0, 500000, 7));
    assert(pattern != network_batch(0, 500000, 42));
    vnet_init(3, 0, 0, 7);
    vnet_observe(observe_network);
    arrivals = inputs = 0; delivered = 0;
    raw_packet(0, 0);
    scp_timer_create(&timer, after_delivery, &fired, 3);
    assert(!vnet_step() && !vnet_step() && !fired);
    assert(!vnet_step() && fired == 1);
    vnet_free();
    native_transfer(TRANSFER_SEND_BACKLOG_BYTES, 1);
    native_transfer(2 * MSS, 1);
    native_transfer(TRANSFER_SEND_BACKLOG_BYTES, 0);
    puts("PASS: shared network modes, 1ms order, duplex serialization, loss replay, copies, native 100ms feedback, MSS and backlog");
    return 0;
}
