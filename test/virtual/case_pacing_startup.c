#include <assert.h>
#include "../../scp.c"

static uint32_t now, sent_at[28];
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
    assert(ntohl(hdr->time_val) == now && sent_count < 28);
    sent_at[sent_count++] = now;
    return (int)len;
}

static void test_fractional_queue(uint32_t start)
{
    struct scp_transport_class transport = {.send = capture};
    struct scp_stream *s;
    struct scp_buf *last;
    unsigned char data[8 * MSS] = {0};
    uint64_t base = (uint64_t)start << SCP_PACING_SHIFT;
    uint64_t quarter = (1ULL << SCP_PACING_SHIFT) / 4;
    unsigned i;

    now = start;
    sent_count = 0;
    s = scp_stream_alloc(&transport, 1, 2);
    assert(s);
    assert(s->pacing_next_time == base);
    s->state = SCP_ESTABLISHED;
    s->srtt = 100;
    s->cwnd = 400 * MSS;
    assert(!scp_send(1, data, sizeof(data)));
    assert(sent_count == 4 && s->t_pacing.active);
    last = container_of(s->snd_q.prev, struct scp_buf, list);
    assert(last->sent_time == base + 7 * quarter);
    assert(!scp_send(1, data, sizeof(data)));
    last = container_of(s->snd_q.prev, struct scp_buf, list);
    assert(last->sent_time == base + 15 * quarter);
    assert(s->pacing_next_time == base + 16 * quarter);
    assert(sent_count == 4 && s->t_pacing.expire == start + 1);
    assert(scp_timer_tree.count == 2);

    for (i = 1; i < 4; ++i) {
        ++now;
        scp_timer_process();
        assert(sent_count == 4 * (i + 1));
    }
    assert(!s->pacing_next && !s->t_pacing.active);
    for (i = 0; i < 16; ++i)
        assert(sent_at[i] == start + (uint32_t)(i / 4));

    assert(!scp_send(1, data, sizeof(data)));
    assert(sent_count == 16 && s->t_pacing.active);
    assert(s->pacing_next->sent_time == base + 16 * quarter);

    now += 3;
    assert(!scp_send(1, data, 4 * MSS));
    assert(sent_count == 28 && !s->pacing_next && !s->t_pacing.active);
    for (i = 16; i < 28; ++i)
        assert(sent_at[i] == now);
    assert(scp_timer_tree.count == 1 && s->t_retrans.active);
    scp_timer_process();
    assert(sent_count == 28);
    scp_stream_free(s);
    assert(!scp_timer_tree.count);
}

static void test_rate_change(uint32_t start)
{
    struct scp_transport_class transport = {.send = capture};
    struct scp_stream *s;
    struct scp_buf *last;
    unsigned char data[MSS] = {0};
    uint64_t base;
    uint64_t ms;

    base = (uint64_t)start << SCP_PACING_SHIFT;
    ms = 1ULL << SCP_PACING_SHIFT;
    now = start;
    sent_count = 0;
    s = scp_stream_alloc(&transport, 1, 2);
    assert(s && s->pacing_next_time == base);
    s->state = SCP_ESTABLISHED;
    s->srtt = 20;
    s->cwnd = 4 * MSS;
    assert(!scp_send(1, data, sizeof(data)));
    assert(sent_count == 1 && sent_at[0] == start);
    assert(s->pacing_next_time == base + 5 * ms);

    now = start + 1;
    s->cwnd = 8 * MSS;
    assert(!scp_send(1, data, sizeof(data)));
    last = container_of(s->snd_q.prev, struct scp_buf, list);
    assert(last->sent_time == base + 5 * ms);
    assert(s->pacing_next_time == base + 7 * ms + ms / 2);
    assert(sent_count == 1);

    s->cwnd = 2 * MSS;
    assert(!scp_send(1, data, sizeof(data)));
    assert(s->schedule_next);
    assert(s->pacing_next_time == base + 7 * ms + ms / 2);
    now = start + 5;
    scp_timer_process();
    assert(sent_count == 2 && sent_at[1] == now);

    now = start + 8;
    s->cwnd = 8 * MSS;
    assert(!scp_output(s));
    last = container_of(s->snd_q.prev, struct scp_buf, list);
    assert(last->sent_time == base + 8 * ms);
    assert(s->pacing_next_time == base + 10 * ms + ms / 2);
    assert(sent_count == 3 && sent_at[2] == now);
    assert(!s->schedule_next && !s->pacing_next);
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
    test_rate_change(0);
    test_rate_change(UINT32_MAX);
    puts("PASS: integer scaling, fractional append, floor dispatch, tail gap, rate change, window credit, idle restart, wrap");
    return 0;
}
