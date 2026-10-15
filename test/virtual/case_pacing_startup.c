#include <assert.h>
#include "../../scp.c"

static uint32_t now, sent_at[8];
static unsigned sent_count;

uint32_t scp_now_time(void)
{
    return now;
}

static int capture(void *user, const void *buf, size_t len)
{
    const struct scp_hdr *hdr = buf;
    (void)user;
    assert(hdr->flags == SCP_FLAG_DATA && len == sizeof(*hdr) + MSS);
    assert(ntohl(hdr->time_val) == now && sent_count < 8);
    sent_at[sent_count++] = now;
    return (int)len;
}

static void test_fractional_queue(uint32_t start)
{
    struct scp_transport_class transport = {.send = capture};
    struct scp_stream *s;
    struct scp_buf *last;
    unsigned char data[4 * MSS] = {0};
    uint64_t base = (uint64_t)start << SCP_PACING_SHIFT;
    uint64_t quarter = (1ULL << SCP_PACING_SHIFT) / 4;
    unsigned i;

    now = start;
    sent_count = 0;
    s = scp_stream_alloc(&transport, 1, 2);
    assert(s);
    s->state = SCP_ESTABLISHED;
    s->srtt = 100;
    s->cwnd = 400 * MSS;
    assert(!scp_send(1, data, sizeof(data)));
    last = container_of(s->snd_q.prev, struct scp_buf, list);
    assert(last->sent_time == base + 3 * quarter);
    assert(!scp_send(1, data, sizeof(data)));
    last = container_of(s->snd_q.prev, struct scp_buf, list);
    assert(last->sent_time == base + 7 * quarter);
    /* Exercise the existing callback at the current millisecond explicitly. */
    scp_timer_delete(&s->t_pacing);
    scp_pacing_timer_cb(s);
    assert(sent_count == 4 && s->t_pacing.active);
    ++now;
    scp_timer_process();
    assert(sent_count == 8 && !s->pacing_next && !s->t_pacing.active);
    for (i = 0; i < 8; ++i)
        assert(sent_at[i] == start + (uint32_t)(i / 4));
    scp_stream_free(s);
    assert(!scp_timer_tree.count);
}

int main(void)
{
    struct scp_stream ss = {0};
    uint64_t ms = 1ULL << SCP_PACING_SHIFT;

    ss.cwnd = MSS;
    ss.rto = 200;
    assert(scp_pacing_time(&ss, MSS) == ms);
    ss.rto = 2000;
    assert(scp_pacing_time(&ss, MSS) == ms);
    ss.cwnd = 2 * MSS;
    assert(scp_pacing_time(&ss, MSS) == ms);

    ss.srtt = 100;
    assert(scp_pacing_time(&ss, MSS) == 50 * ms);
    ss.cwnd = MSS;
    assert(scp_pacing_time(&ss, MSS) == 100 * ms);
    assert(scp_pacing_time(&ss, 1) > 0 && scp_pacing_time(&ss, 1) < ms);
    assert(!scp_init(4));
    test_fractional_queue(0);
    test_fractional_queue(UINT32_MAX);
    puts("PASS: startup interval, fractional queue append, floor dispatch, millisecond wrap");
    return 0;
}
