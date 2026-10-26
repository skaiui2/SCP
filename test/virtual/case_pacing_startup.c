#include <assert.h>
#ifndef CWND_WIN_MAX
#define CWND_WIN_MAX (1U << 30)
#endif
#include "../../scp.c"

static uint32_t now, sent_at[28];
static unsigned sent_count;

uint32_t scp_now_time(void)
{
    return now;
}

static uint32_t unit_gain(struct scp_stream *ss)
{
    (void)ss;
    return SCP_GAIN_ONE_Q16;
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
    s->cc_pacing_gain = unit_gain;
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
    s->cc_pacing_gain = unit_gain;
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
    assert(s->t_pacing.expire == start + 5 && s->t_pacing.timeout == 4);

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
    assert(!s->schedule_next && !s->pacing_next && !s->t_pacing.active);
    scp_stream_free(s);
    assert(!scp_timer_tree.count);
}

static void test_pacing_gain(void)
{
    struct scp_stream ss = {0};
    uint64_t ms;
    uint64_t gap;

    ms = 1ULL << SCP_PACING_SHIFT;
    assert(!scp_install_cc(&ss, SCP_CC_AIMD));
    assert(ss.cc_pacing_gain == scp_aimd_pacing_gain);
    assert(ss.cc_pacing_gain(&ss) == 90852U);
    assert(scp_pacing_time(&ss, MSS) == ms * SCP_TICK_SCALE);
    ss.srtt = 100;
    assert(scp_pacing_time(&ss, MSS) == UINT64_C(309817039400));

    ss.ssthresh = MSS;
    assert(ss.cc_pacing_gain(&ss) == SCP_GAIN_CA_Q16);
    gap = scp_pacing_time(&ss, MSS);
    assert(gap == UINT64_C(260629804900));
    assert(gap % ms != 0);
    ss.ssthresh = 4 * MSS;
    ss.cwnd = 3 * MSS;
    assert(ss.cc_pacing_gain(&ss) == 90852U);
    ss.cwnd = ss.ssthresh;
    assert(ss.cc_pacing_gain(&ss) == SCP_GAIN_CA_Q16);
    ss.cwnd = 10 * MSS;
    assert(ss.cc_pacing_gain(&ss) == SCP_GAIN_CA_Q16);
    assert(scp_pacing_time(&ss, MSS) == UINT64_C(26062980490));

    assert(!scp_install_cc(&ss, SCP_CC_PROB));
    assert(ss.cc_pacing_gain == scp_prob_pacing_gain);
    ss.srtt = 100;
    ss.cwnd = MSS;
    assert(ss.cc_pacing_gain(&ss) == SCP_GAIN_CA_Q16);
    ss.ssthresh = 100 * MSS;
    assert(ss.cc_pacing_gain(&ss) == SCP_GAIN_CA_Q16);
    assert(scp_pacing_time(&ss, MSS) == UINT64_C(260629804900));
    ss.cwnd = 10 * MSS;
    assert(ss.cc_pacing_gain(&ss) == SCP_GAIN_CA_Q16);
    assert(scp_pacing_time(&ss, MSS) == UINT64_C(26062980490));
}

static void test_timer_due(uint32_t start)
{
    struct scp_stream ss = {0};
    struct scp_buf packet = {0};
    uint64_t tick = 1ULL << SCP_PACING_SHIFT;
    uint64_t base = (uint64_t)start << SCP_PACING_SHIFT;

    now = start;
    ss.pacing_next = &packet;
    packet.sent_time = base + 25 * tick + tick / 2;
    scp_pacing_arm_timer(&ss);
    assert(ss.t_pacing.active && ss.t_pacing.expire == start + 25);
    assert(ss.t_pacing.timeout == 25 && scp_timer_tree.count == 1);
    now = start + 1;
    scp_pacing_arm_timer(&ss);
    assert(ss.t_pacing.expire == start + 25 && ss.t_pacing.timeout == 25);
    assert(scp_timer_tree.count == 1);

    packet.sent_time = base + 30 * tick;
    scp_pacing_arm_timer(&ss);
    assert(ss.t_pacing.expire == start + 30 && ss.t_pacing.timeout == 29);
    packet.sent_time = base;
    scp_pacing_arm_timer(&ss);
    assert(ss.t_pacing.expire == now + 1 && ss.t_pacing.timeout == 1);
    packet.sent_time = (uint64_t)now << SCP_PACING_SHIFT;
    scp_pacing_arm_timer(&ss);
    assert(ss.t_pacing.expire == now + 1 && ss.t_pacing.timeout == 1);
    ss.pacing_next = NULL;
    scp_pacing_arm_timer(&ss);
    assert(!ss.t_pacing.active && !scp_timer_tree.count);
}

static void test_prob_time_units(void)
{
    struct scp_stream ss = {0};

    assert(!scp_prob_configure(&ss, 100, 8000));
    assert(ss.prob_beta_q16 == 1817 && ss.prob_gamma_q16 == -134460);
    ss.rtt_base = 100U * SCP_TICK_SCALE;
    ss.srtt = 150U * SCP_TICK_SCALE;
    scp_md_prob(&ss);
    assert(ss.d == 50 * 65536);
#if SCP_TICK_SCALE == 1000U
    ss.srtt = ss.rtt_base + SCP_TICK_SCALE / 2;
    scp_md_prob(&ss);
    assert(ss.d == 32768);
#endif
    ss.p_ema = 65535;
    ss.rtt_sample = 100U * SCP_TICK_SCALE;
    assert(scp_prob_on_rtt(&ss) == 100U * SCP_TICK_SCALE +
           (uint32_t)(((uint64_t)65535 * SCP_RTO_MIN) >> 16));
}

static void test_ca_credit(void)
{
    struct scp_stream ss = {0};
    uint32_t acked[] = {MSS, 584, 2 * MSS, 3 * MSS + 584, 0};
    uint32_t before, high;
    uint64_t credit;
    unsigned i;

    ss.cwnd_cnt = 17;
    scp_aimd_init(&ss);
    assert(ss.cwnd == MSS && !ss.cwnd_cnt);
    ss.ssthresh = MSS;
    ss.cwnd = 4 * MSS;
    for (i = 0; i < 200; ++i) {
        before = ss.cwnd;
        credit = ((uint64_t)MSS * acked[i % 5]) + ss.cwnd_cnt;
        scp_aimd_on_ack(&ss, acked[i % 5]);
        assert(ss.cwnd >= before && ss.cwnd_cnt < before);
        assert(((uint64_t)(ss.cwnd - before) * before) + ss.cwnd_cnt == credit);
    }

    high = 3U * MSS * MSS;
    ss.cwnd = high;
    ss.cwnd_cnt = 0;
    scp_aimd_on_ack(&ss, MSS);
    assert(ss.cwnd == high && ss.cwnd_cnt == MSS * MSS);
    scp_aimd_on_ack(&ss, MSS);
    assert(ss.cwnd == high && ss.cwnd_cnt == 2U * MSS * MSS);
    scp_aimd_on_ack(&ss, MSS);
    assert(ss.cwnd == high + 1 && !ss.cwnd_cnt);
    ss.cwnd = high;
    scp_aimd_on_ack(&ss, high);
    assert(ss.cwnd == high + MSS && !ss.cwnd_cnt);

    ss.cwnd = CWND_WIN_MAX - 1;
    ss.cwnd_cnt = 0;
    scp_aimd_on_ack(&ss, (2U * CWND_WIN_MAX / MSS) + 1);
    assert(ss.cwnd == CWND_WIN_MAX && ss.cwnd_cnt < ss.cwnd);

    ss.cwnd = 20 * MSS;
    ss.cwnd_cnt = 17;
    scp_aimd_on_loss(&ss);
    assert(ss.cwnd == 10 * MSS && ss.ssthresh == 10 * MSS && !ss.cwnd_cnt);
    ss.cwnd = 20 * MSS;
    ss.cwnd_cnt = 17;
    scp_aimd_on_timeout(&ss);
    assert(ss.cwnd == MSS && ss.ssthresh == 10 * MSS && !ss.cwnd_cnt);
    scp_aimd_init(&ss);
    for (i = 0; i < 4; ++i) {
        before = ss.cwnd;
        scp_aimd_on_ack(&ss, acked[i]);
        assert(ss.cwnd == before + acked[i] && !ss.cwnd_cnt);
    }
}

int main(void)
{
    struct scp_stream ss = {0};
    uint64_t ms = 1ULL << SCP_PACING_SHIFT;

    ss.cc_pacing_gain = unit_gain;
    ss.cwnd = MSS;
    ss.rto = SCP_RTO_MIN;
    assert(scp_pacing_time(&ss, MSS) == ms * SCP_TICK_SCALE);
    ss.rto = SCP_RTO_MAX;
    assert(scp_pacing_time(&ss, MSS) == ms * SCP_TICK_SCALE);
    ss.cwnd = 2 * MSS;
    assert(scp_pacing_time(&ss, MSS) == ms * SCP_TICK_SCALE);

    ss.srtt = 100;
    assert(scp_pacing_time(&ss, MSS) == 50 * ms);
    ss.cwnd = MSS;
    assert(scp_pacing_time(&ss, MSS) == 100 * ms);
    assert(scp_pacing_time(&ss, 1) > 0 && scp_pacing_time(&ss, 1) < ms);
    test_pacing_gain();
    test_prob_time_units();
    test_ca_credit();
    assert(!scp_init(4));
    test_fractional_queue(0);
    test_fractional_queue(UINT32_MAX);
    test_rate_change(0);
    test_rate_change(UINT32_MAX);
    test_timer_due(0);
    test_timer_due(UINT32_MAX - 10);
    puts("PASS: integer scaling, fractional append, floor dispatch, tail gap, on-demand expiry, rate change, CA credit carry/reset, window credit, idle restart, wrap");
    return 0;
}
