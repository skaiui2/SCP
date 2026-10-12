#include "scp.h"
#include "vnet.h"
#include "research.h"
#include "../common/transfer.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
extern uint32_t scp_now_time(void);
extern uint32_t scp_experiment_g_hundredths, scp_experiment_first_data_ms;
extern uint32_t scp_experiment_ack_calls;
extern uint64_t scp_experiment_acked_bytes;
int main(int argc, char **argv)
{
    uint64_t total = UINT64_C(1073741824), sent = 0, received = 0;
    uint64_t hash_a = UINT64_C(14695981039346656037), hash_b = hash_a;
    uint32_t delay = 50, rate = 1000, loss = 0, seed = 1, timeout = 3600000;
    uint32_t data_acked = 0, received_complete = 0, observed_end;
    uint32_t max_cwnd = 0, max_flight = 0, max_backlog = 0;
    uint32_t min_snd_wnd = UINT32_MAX, min_rcv_wnd = UINT32_MAX;
    struct vnet_endpoint ep_a = {0}, ep_b = {1};
    struct scp_transport_class ta = {.send = research_send, .user = &ep_a};
    struct scp_transport_class tb = {.send = vnet_send, .user = &ep_b};
    struct transfer a, b;
    FILE *summary;
    int i, ok = 0;
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
        else if (!strcmp(argv[i], "--g-hundredths")) scp_experiment_g_hundredths = (uint32_t)value;
        else return 2;
    }
    if (!total || total > (1U << 30) || rate != 1000 || delay != 50 || loss || seed != 1 ||
        !scp_experiment_g_hundredths || !timeout) return 2;
    summary = fopen("summary.json", "w");
    if (!summary) return 2;
    vnet_init(delay, rate, loss, seed);
    if (scp_init(16)) return 2;
    if (transfer_init(&b, &tb, 2, 1, 0, total, SCP_CC_AIMD) ||
        transfer_init(&a, &ta, 1, 2, 1, total, SCP_CC_AIMD)) return 2;
    vnet_attach(&a, &b);
    printf("Generated transfer: %" PRIu64 " bytes, %u Mbps/direction, %u ms one-way, %u ppm loss, seed %u\n", total, rate, delay, loss, seed);
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
    fprintf(summary, "{\"success\":%s,\"requested_bytes\":%" PRIu64 ",\"accepted_bytes\":%" PRIu64 ",\"received_bytes\":%" PRIu64 ",\"checksum_algorithm\":\"FNV-1a-64\",\"checksum_a\":\"%016" PRIx64 "\",\"checksum_b\":\"%016" PRIx64 "\",\"simulated_ms\":%u,\"mbps\":%u,\"delay_ms\":%u,\"loss_ppm\":%u,\"seed\":%u,\"packets\":%" PRIu64 ",\"dropped\":%" PRIu64 "}\n", ok ? "true" : "false", total, sent, received, hash_a, hash_b, scp_now_time(), rate, delay, loss, seed, vnet_packets, vnet_dropped);
    printf("%s: received=%" PRIu64 " A=%016" PRIx64 " B=%016" PRIx64 " virtual_ms=%u dropped=%" PRIu64 "\n", ok ? "PASS" : "FAIL", received, hash_a, hash_b, scp_now_time(), vnet_dropped);
    fclose(summary);
    summary = fopen("research_summary.json", "w");
    if (!summary) return 2;
    fprintf(summary, "{\"status\":\"%s\",\"received_bytes\":%" PRIu64 ",\"accepted_bytes\":%" PRIu64 ",\"checksum_ok\":%s,\"checksum_a\":\"%016" PRIx64 "\",\"checksum_b\":\"%016" PRIx64 "\",\"dropped\":%" PRIu64 ",\"last_observed_ms\":%u,\"first_data_tx_ms\":",
        ok ? "complete" : (scp_now_time() >= timeout ? "sim_timeout" : "failed"), received, sent,
        hash_a == hash_b ? "true" : "false", hash_a, hash_b, vnet_dropped, observed_end);
    if (scp_experiment_first_data_ms == UINT32_MAX) fprintf(summary, "null");
    else fprintf(summary, "%u", scp_experiment_first_data_ms);
    fprintf(summary, ",\"data_acked_ms\":");
    if (data_acked) fprintf(summary, "%u", data_acked); else fprintf(summary, "null");
    fprintf(summary, ",\"receiver_payload_complete_ms\":%u,\"last_data_tx_ms\":%u,\"close_kind\":\"%s\",\"simulated_ms\":%u,\"g_hundredths\":%u,\"new_bytes\":%u,\"new_packets\":%" PRIu64 ",\"retrans_packets\":%" PRIu64 ",\"retrans_bytes\":%" PRIu64 ",\"observer_errors\":%u,\"acked_bytes\":%" PRIu64 ",\"ack_calls\":%u,\"max_cwnd\":%u,\"max_flight\":%u,\"max_backlog\":%u,\"min_snd_wnd\":%u,\"min_rcv_wnd\":%u}\n",
        received_complete, research_last_data_ms, a.done && b.done ? "both_closed" : "incomplete",
        scp_now_time(), scp_experiment_g_hundredths, research_new_bytes, research_new_packets,
        research_retrans_packets, research_retrans_bytes, research_errors, scp_experiment_acked_bytes,
        scp_experiment_ack_calls, max_cwnd, max_flight, max_backlog, min_snd_wnd, min_rcv_wnd);
    fclose(summary);
    vnet_free();
    return ok ? 0 : 1;
}
