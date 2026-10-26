#include "scp.h"
#include "../virtual/vnet.h"
#include "observe.h"
#include "../common/transfer.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
extern uint32_t scp_now_time(void);
#if SCP_RESEARCH_GAIN_Q16
static uint32_t research_fixed_gain(struct scp_stream *ss)
{
    (void)ss;
    return SCP_RESEARCH_GAIN_Q16;
}
#endif

int main(int argc, char **argv)
{
    uint64_t total = UINT64_C(1073741824), sent = 0, received = 0;
    uint64_t hash_a = UINT64_C(14695981039346656037), hash_b = hash_a;
    uint32_t delay = 50, rate = 0, loss = 0, seed = 1, timeout = 3600000;
    uint32_t data_acked = 0, received_complete = 0, observed_end;
    uint32_t max_cwnd = 0, max_flight = 0, max_backlog = 0;
    uint32_t min_snd_wnd = UINT32_MAX, min_rcv_wnd = UINT32_MAX;
    uint32_t initial_cwnd, initial_ssthresh;
    struct vnet_endpoint ep_a = {0}, ep_b = {1};
    struct scp_transport_class ta = {.send = vnet_send, .user = &ep_a};
    struct scp_transport_class tb = {.send = vnet_send, .user = &ep_b};
    struct transfer a, b;
    FILE *summary;
    int i, payload_separator = 0, ok = 0;
    for (i = 1; i < argc; i += 2) {
        uint64_t value;
        char *end;
        if (i + 1 >= argc) return 2;
        value = strtoull(argv[i + 1], &end, 10);
        if (*end) return 2;
        if (!strcmp(argv[i], "--bytes")) total = value;
        else if (!strcmp(argv[i], "--delay-ms")) delay = (uint32_t)value;
        else if (!strcmp(argv[i], "--mbps")) rate = (uint32_t)value;
        else if (!strcmp(argv[i], "--loss-ppm")) loss = (uint32_t)value;
        else if (!strcmp(argv[i], "--seed")) seed = (uint32_t)value;
        else if (!strcmp(argv[i], "--timeout-ms")) timeout = (uint32_t)value;
        else return 2;
    }
    if (!total || total > (1U << 30) || delay != 50 || rate || loss || seed != 1 ||
        !timeout || timeout > UINT32_MAX / SCP_TICK_SCALE) return 2;
    timeout *= SCP_TICK_SCALE;
    summary = fopen("summary.json", "w");
    if (!summary) return 2;
    vnet_init(delay, rate, loss, seed);
    vnet_observe(research_observe);
    if (scp_init(16)) return 2;
    if (transfer_init(&b, &tb, 2, 1, 0, total, SCP_CC_AIMD) ||
        transfer_init(&a, &ta, 1, 2, 1, total, SCP_CC_AIMD)) return 2;
#if SCP_RESEARCH_CA
    a.stream->ssthresh = MSS;
    b.stream->ssthresh = MSS;
#endif
#if SCP_RESEARCH_GAIN_Q16
    a.stream->cc_pacing_gain = research_fixed_gain;
    b.stream->cc_pacing_gain = research_fixed_gain;
#endif
    vnet_attach(&a, &b);
    initial_cwnd = a.stream->cwnd;
    initial_ssthresh = a.stream->ssthresh;
    research_sender = a.stream;
#if SCP_RESEARCH_CA
    a.send_backlog_bytes = 8U << 20;
#else
    a.send_backlog_bytes = 1U << 30;
#endif
    printf("Generated SCP transfer: %" PRIu64 " bytes, %s, %u ms one-way, loss %u ppm, seed %u\n",
        total, rate ? "serialized link" : "fixed delay without serialization", delay, loss, seed);
    while (scp_now_time() < timeout) {
        if (vnet_step()) break;
        if (!scp_is_closed(a.fd)) {
            uint32_t flight = a.stream->snd_nxt - a.stream->snd_una;
            uint32_t backlog = a.stream->snd_seq_q - a.stream->snd_una;
            if (a.stream->cwnd > max_cwnd) max_cwnd = a.stream->cwnd;
            if (flight > max_flight) max_flight = flight;
            if (backlog > max_backlog) max_backlog = backlog;
            if (a.stream->snd_wnd < min_snd_wnd) min_snd_wnd = a.stream->snd_wnd;
            if (a.stream->rcv_wnd < min_rcv_wnd) min_rcv_wnd = a.stream->rcv_wnd;
            if (!data_acked && a.sent == total && a.stream->snd_una == a.stream->snd_seq_q)
                data_acked = scp_now_time();
        }
        if (transfer_step(&a) || transfer_step(&b) || research_errors) break;
        if (!received_complete && b.received == total) received_complete = scp_now_time();
        if (a.done && b.done) { ok = 1; break; }
    }
    sent = a.sent; received = b.received; hash_a = a.hash; hash_b = b.hash;
    observed_end = data_acked;
    if (!observed_end) {
        vnet_step();
        observed_end = scp_now_time();
    }
    if (research_finish(observed_end)) ++research_errors;
    ok = ok && data_acked && received == total && sent == total && hash_a == hash_b && !research_errors;
    transfer_cleanup(&a); transfer_cleanup(&b);
    fprintf(summary, "{\"success\":%s,\"requested_bytes\":%" PRIu64 ",\"accepted_bytes\":%" PRIu64 ",\"received_bytes\":%" PRIu64 ",\"checksum_algorithm\":\"FNV-1a-64\",\"checksum_a\":\"%016" PRIx64 "\",\"checksum_b\":\"%016" PRIx64 "\",\"simulated_ms\":%.3f,\"mbps\":%u,\"delay_ms\":%u,\"loss_ppm\":%u,\"seed\":%u,\"packets\":%" PRIu64 ",\"dropped\":%" PRIu64 "}\n", ok ? "true" : "false", total, sent, received, hash_a, hash_b, (double)scp_now_time() / SCP_TICK_SCALE, rate, delay, loss, seed, vnet_packets, vnet_dropped);
    printf("%s: received=%" PRIu64 " A=%016" PRIx64 " B=%016" PRIx64 " virtual_ms=%.3f dropped=%" PRIu64 "\n", ok ? "PASS" : "FAIL", received, hash_a, hash_b, (double)scp_now_time() / SCP_TICK_SCALE, vnet_dropped);
    fclose(summary);
    summary = fopen("research_summary.json", "w");
    if (!summary) return 2;
    fprintf(summary, "{\"initial_cwnd_bytes\":%u,\"initial_ssthresh_bytes\":%u,\"tick_scale\":%u,\"clock_tick_ms\":%.3f,",
        initial_cwnd, initial_ssthresh, (unsigned)SCP_TICK_SCALE, 1.0 / SCP_TICK_SCALE);
    fprintf(summary, "\"ca_update\":\"byte-remainder\",\"mode\":\"%s\",\"gain_policy\":\"%s\",\"gain_q16\":",
        SCP_RESEARCH_CA ? "always_congestion_avoidance" : "always_slow_start",
        SCP_RESEARCH_GAIN_Q16 ? "fixed_q16" : "native_aimd");
    if (SCP_RESEARCH_GAIN_Q16) fprintf(summary, "%u", (unsigned)SCP_RESEARCH_GAIN_Q16);
    else fprintf(summary, "null");
    fprintf(summary, ",\"engine\":\"scp_virtual\",\"transport\":\"%s\",\"rate_mbps\":%u,\"delay_ms\":%u,\"loss_ppm\":%u,\"seed\":%u,\"feedback_count\":%" PRIu64 ",\"feedback_min_ms\":%.3f,\"feedback_max_ms\":%.3f,",
        rate ? "serialized_link" : "fixed_delay", rate, delay, loss, seed,
        research_feedback_count, (double)research_feedback_min_ticks / SCP_TICK_SCALE,
        (double)research_feedback_max_ticks / SCP_TICK_SCALE);
    fprintf(summary, "\"mss_bytes\":%u,\"application_backlog_bytes\":%u,\"cwnd_limit_bytes\":%u,\"send_window_bytes\":%u,\"recv_window_bytes\":%u,",
        (unsigned)MSS, a.send_backlog_bytes, CWND_WIN_MAX, SEND_WIN_INIT, RECV_WIN_INIT);
    fprintf(summary, "\"status\":\"%s\",\"received_bytes\":%" PRIu64 ",\"accepted_bytes\":%" PRIu64 ",\"checksum_ok\":%s,\"checksum_a\":\"%016" PRIx64 "\",\"checksum_b\":\"%016" PRIx64 "\",\"dropped\":%" PRIu64 ",\"last_observed_ms\":%.3f,\"first_data_tx_ms\":",
        ok ? "complete" : (scp_now_time() >= timeout ? "sim_timeout" : "failed"), received, sent,
        hash_a == hash_b ? "true" : "false", hash_a, hash_b, vnet_dropped, (double)observed_end / SCP_TICK_SCALE);
    if (research_first_data_tick == UINT32_MAX) fprintf(summary, "null");
    else fprintf(summary, "%.3f", (double)research_first_data_tick / SCP_TICK_SCALE);
    fprintf(summary, ",\"data_acked_ms\":");
    if (data_acked) fprintf(summary, "%.3f", (double)data_acked / SCP_TICK_SCALE); else fprintf(summary, "null");
    fprintf(summary, ",\"receiver_payload_complete_ms\":%.3f,\"last_data_tx_ms\":%.3f,\"close_kind\":\"%s\",\"simulated_ms\":%.3f,\"new_bytes\":%u,\"new_packets\":%" PRIu64 ",\"retrans_packets\":%" PRIu64 ",\"retrans_bytes\":%" PRIu64 ",\"observer_errors\":%u,\"max_cwnd\":%u,\"max_flight\":%u,\"max_backlog\":%u,\"min_snd_wnd\":%u,\"min_rcv_wnd\":%u,",
        (double)received_complete / SCP_TICK_SCALE, (double)research_last_data_tick / SCP_TICK_SCALE, a.done && b.done ? "both_closed" : "incomplete",
        (double)scp_now_time() / SCP_TICK_SCALE, research_new_bytes, research_new_packets,
        research_retrans_packets, research_retrans_bytes, research_errors, max_cwnd, max_flight, max_backlog, min_snd_wnd, min_rcv_wnd);
    fprintf(summary, "\"first_data_tx_tick\":%u,\"data_acked_tick\":%u,\"last_observed_tick\":%u,\"last_data_tx_tick\":%u,",
        research_first_data_tick, data_acked, observed_end, research_last_data_tick);
    fprintf(summary, "\"nonfinal_short_packets\":%" PRIu64 ",\"last_new_payload_bytes\":%u,\"data_payload_packet_counts\":{",
        research_nonfinal_short_packets, research_last_new_payload_bytes);
    for (i = 1; i <= MSS; ++i) {
        if (!research_payload_packets[i]) continue;
        fprintf(summary, "%s\"%d\":%" PRIu64, payload_separator ? "," : "", i, research_payload_packets[i]);
        payload_separator = 1;
    }
    fprintf(summary, "}}\n");
    fclose(summary);
    vnet_free();
    return ok ? 0 : 1;
}
