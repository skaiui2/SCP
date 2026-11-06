#include "sscp.h"
#include "../virtual/vnet.h"
#include "../common/transfer.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>

#if defined(_WIN32)
#include <winsock2.h>
#else
#include <arpa/inet.h>
#endif

extern uint32_t sscp_now_time(void);

static FILE *s_events_file = NULL;
static uint32_t s_fixed_gain_q16 = 0;

static uint32_t research_fixed_gain(struct sscp_stream *ss)
{
    (void)ss;
    return s_fixed_gain_q16;
}

static void research_on_event(unsigned direction, int event, const void *buf, size_t len, uint32_t now)
{
    if (event != VNET_SEND || len < sizeof(struct sscp_hdr) || !s_events_file) {
        return;
    }
    const struct sscp_hdr *h = (const struct sscp_hdr *)buf;
    uint32_t seq = ntohl(h->seq);
    uint32_t ack = ntohl(h->ack);
    uint16_t plen = ntohs(h->len);
    double t_ms = (double)now / SSCP_TICK_SCALE;
    fprintf(s_events_file, "%u,%d,%.3f,%u,%u,%u,%u,%u\n",
            direction, event, t_ms, now, (unsigned)h->flags, seq, ack, (unsigned)plen);
}

int main(int argc, char **argv)
{
    uint64_t total = 1073741824;
    uint32_t gain_q16 = 0;
    const char *mode = "ca";
    uint32_t delay_ms = 50;
    uint32_t timeout_ms = 600000;
    const char *events_path = "network_events.csv";
    const char *summary_path = "summary.json";

    for (int i = 1; i < argc; i += 2) {
        if (i + 1 >= argc) {
            fprintf(stderr, "Missing value for argument: %s\n", argv[i]);
            return 2;
        }
        if (!strcmp(argv[i], "--gain-q16")) {
            gain_q16 = (uint32_t)strtoul(argv[i + 1], NULL, 10);
        } else if (!strcmp(argv[i], "--mode")) {
            mode = argv[i + 1];
        } else if (!strcmp(argv[i], "--bytes")) {
            total = (uint64_t)strtoull(argv[i + 1], NULL, 10);
        } else if (!strcmp(argv[i], "--delay-ms")) {
            delay_ms = (uint32_t)strtoul(argv[i + 1], NULL, 10);
        } else if (!strcmp(argv[i], "--timeout-ms")) {
            timeout_ms = (uint32_t)strtoul(argv[i + 1], NULL, 10);
        } else if (!strcmp(argv[i], "--events")) {
            events_path = argv[i + 1];
        } else if (!strcmp(argv[i], "--summary")) {
            summary_path = argv[i + 1];
        } else {
            fprintf(stderr, "Unknown argument: %s\n", argv[i]);
            return 2;
        }
    }

    s_fixed_gain_q16 = gain_q16;

    s_events_file = fopen(events_path, "w");
    if (!s_events_file) {
        fprintf(stderr, "Failed to open events file: %s\n", events_path);
        return 1;
    }
    fprintf(s_events_file, "direction,event,t_ms,t_tick,flags,seq,ack,payload_bytes\n");

    vnet_init(delay_ms, 0, 0, 1);
    vnet_observe(research_on_event);

    if (sscp_init(16) != 0) {
        fprintf(stderr, "Failed to initialize sscp\n");
        fclose(s_events_file);
        vnet_free();
        return 1;
    }

    struct vnet_endpoint ep_a = {.direction = 0};
    struct vnet_endpoint ep_b = {.direction = 1};
    struct sscp_transport_class ta = {.send = vnet_send, .user = &ep_a};
    struct sscp_transport_class tb = {.send = vnet_send, .user = &ep_b};
    struct transfer a, b;

    if (transfer_init(&b, &tb, 2, 1, 0, total, SSCP_CC_AIMD) != 0 ||
        transfer_init(&a, &ta, 1, 2, 1, total, SSCP_CC_AIMD) != 0) {
        fprintf(stderr, "Failed to initialize transfer\n");
        fclose(s_events_file);
        vnet_free();
        return 1;
    }

    int is_ca = (strcmp(mode, "ss") != 0);
    if (is_ca) {
        a.stream->cwnd = a.stream->ssthresh = 10U * MSS;
        b.stream->cwnd = b.stream->ssthresh = 10U * MSS;
        a.send_backlog_bytes = total < (8U << 20) ? (uint32_t)total : (8U << 20);
    } else {
        a.stream->cwnd = 1U * MSS;
        a.stream->ssthresh = 1U << 30;
        b.stream->cwnd = 1U * MSS;
        b.stream->ssthresh = 1U << 30;
        a.send_backlog_bytes = (uint32_t)total;
    }

    if (s_fixed_gain_q16 > 0) {
        a.stream->cc_pacing_gain = research_fixed_gain;
        b.stream->cc_pacing_gain = research_fixed_gain;
    }

    vnet_attach(&a, &b);

    uint32_t timeout_ticks = timeout_ms * SSCP_TICK_SCALE;
    uint32_t max_cwnd = a.stream->cwnd;
    int ok = 0;

    while (sscp_now_time() < timeout_ticks) {
        if (vnet_step()) break;
        if (a.stream->cwnd > max_cwnd) {
            max_cwnd = a.stream->cwnd;
        }
        if (transfer_step(&a) || transfer_step(&b)) break;
        if (a.done && b.done) {
            ok = 1;
            break;
        }
    }

    uint32_t end_ticks = sscp_now_time();
    double duration_ms = (double)end_ticks / SSCP_TICK_SCALE;
    double mbps = (duration_ms > 0.0) ? ((double)b.received * 8.0 / (duration_ms * 1000.0)) : 0.0;

    fclose(s_events_file);
    s_events_file = NULL;

    FILE *sf = fopen(summary_path, "w");
    if (sf) {
        fprintf(sf, "{\n"
                    "  \"success\": %s,\n"
                    "  \"mode\": \"%s\",\n"
                    "  \"gain_q16\": %u,\n"
                    "  \"bytes\": %" PRIu64 ",\n"
                    "  \"duration_ms\": %.3f,\n"
                    "  \"mbps\": %.2f,\n"
                    "  \"max_cwnd\": %u,\n"
                    "  \"packets\": %" PRIu64 ",\n"
                    "  \"dropped\": %" PRIu64 "\n"
                    "}\n",
                ok ? "true" : "false", mode, gain_q16, b.received,
                duration_ms, mbps, max_cwnd, vnet_packets, vnet_dropped);
        fclose(sf);
    }

    printf("{\"success\":%s,\"mode\":\"%s\",\"gain_q16\":%u,\"bytes\":%" PRIu64 ",\"duration_ms\":%.3f,\"mbps\":%.2f,\"max_cwnd\":%u}\n",
           ok ? "true" : "false", mode, gain_q16, b.received, duration_ms, mbps, max_cwnd);

    transfer_cleanup(&a);
    transfer_cleanup(&b);
    vnet_free();

    return ok ? 0 : 1;
}
