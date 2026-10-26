#include "scp.h"
#include "vnet.h"
#include "../common/transfer.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
static int64_t core_live;
static void *count_malloc(size_t size)
{
    void *p = malloc(size);
    if (p) ++core_live;
    return p;
}
static void count_free(void *p)
{
    if (p) --core_live;
    free(p);
}
#define malloc count_malloc
#define free count_free
#include "../../scp.c"
#undef malloc
#undef free

struct peak { uint32_t cwnd; uint64_t tick; };
static struct peak *peaks;
static size_t peak_count, peak_capacity;
static struct transfer *sender;
static uint64_t clock_tick, first_data_tick, retrans_packets, timeouts[2];
static uint64_t srtt_samples, srtt_sum;
static uint32_t srtt_max, highest_seq[2];
static uint64_t wire_bytes[2];
static int data_seen[2], sequence_ok = 1;
static void (*timeout_handler[2])(struct scp_stream *);

static void count_timeout(struct scp_stream *ss)
{
    ++timeouts[ss->src_fd - 1];
    timeout_handler[ss->src_fd - 1](ss);
}

static void record_peak(struct scp_stream *s)
{
    if (peak_count && s->cwnd <= peaks[peak_count - 1].cwnd) return;
    if (peak_count == peak_capacity) {
        struct peak *p;
        peak_capacity = peak_capacity ? peak_capacity * 2 : 256;
        p = realloc(peaks, peak_capacity * sizeof(*p));
        if (!p) abort();
        peaks = p;
    }
    peaks[peak_count].cwnd = s->cwnd;
    peaks[peak_count++].tick = clock_tick;
}

static void observe(unsigned direction, int event, const void *buf, size_t len, uint32_t now)
{
    const struct scp_hdr *h = buf;
    struct scp_stream *s;
    uint32_t seq, bytes;
    (void)now;
    if (len < sizeof(*h)) { sequence_ok = 0; return; }
    bytes = ntohs(h->len);
    seq = ntohl(h->seq);
    if (event == VNET_SEND && (h->flags & SCP_FLAG_DATA)) {
        if (!data_seen[direction]) {
            highest_seq[direction] = seq;
            data_seen[direction] = 1;
            if (!direction) {
                first_data_tick = clock_tick;
                if (sender) record_peak(sender->stream);
            }
        }
        if (len != sizeof(*h) + bytes || !bytes) sequence_ok = 0;
        if (SEQ_LT(seq, highest_seq[direction])) ++retrans_packets;
        else {
            if (seq != highest_seq[direction]) sequence_ok = 0;
            highest_seq[direction] = seq + bytes;
            wire_bytes[direction] += bytes;
        }
    }
    if (direction != 1 || event != VNET_INPUT_RETURNED || !(h->flags & SCP_FLAG_ACK)) return;
    s = hashmap_get(&scp_stream_map, (void *)(uintptr_t)1);
    if (!s || !sender) return;
    if (s->srtt) {
        srtt_sum += s->srtt;
        ++srtt_samples;
        if (s->srtt > srtt_max) srtt_max = s->srtt;
    }
    record_peak(s);
}

static void check_sequences(void)
{
    struct list_node *node;
    struct scp_stream *s;
    uint32_t end;
    for (node = scp_stream_queue.next; node != &scp_stream_queue; node = node->next) {
        s = container_of(node, struct scp_stream, node);
        end = s->snd_seq_q;
        if (s->state == SCP_FIN_WAIT || s->state == SCP_LAST_ACK) ++end;
        if (SEQ_GT(s->snd_una, s->snd_nxt) || SEQ_GT(s->snd_nxt, end)) sequence_ok = 0;
    }
}
extern uint32_t scp_now_time(void);
int main(int argc, char **argv)
{
    uint64_t total = UINT64_C(1073741824), sent = 0, received = 0, steps = 0;
    uint64_t hash_a = UINT64_C(14695981039346656037), hash_b = hash_a;
    uint32_t delay = 10, rate = 100, loss = 0, seed = 1, timeout = 36000000;
    uint32_t backlog = TRANSFER_SEND_BACKLOG_BYTES;
    uint32_t queue_packets = 0;
    double srtt_avg, startup = -1;
    int checksum_ok, cleanup_ok;
    size_t peak_index;
    struct vnet_endpoint ep_a = {0}, ep_b = {1};
    struct scp_transport_class ta = {.send = vnet_send, .user = &ep_a};
    struct scp_transport_class tb = {.send = vnet_send, .user = &ep_b};
    struct transfer a, b;
    FILE *summary;
    int i, ok = 0, cc = SCP_CC_AIMD;
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
        else if (!strcmp(argv[i], "--queue-packets")) {
            if (value > UINT32_MAX) return 2;
            queue_packets = (uint32_t)value;
        }
        else if (!strcmp(argv[i], "--timeout-ms")) timeout = (uint32_t)value;
        else if (!strcmp(argv[i], "--cc")) cc = (int)value;
        else if (!strcmp(argv[i], "--send-backlog-bytes")) {
            if (value < MSS || value > UINT32_MAX) return 2;
            backlog = (uint32_t)value;
        }
        else return 2;
    }
    if (!total || loss > 1000000 || !timeout || (cc != SCP_CC_AIMD && cc != SCP_CC_PROB)) return 2;
    summary = fopen("summary.json", "w");
    if (!summary) return 2;
    vnet_init(delay, rate, loss, seed);
    vnet_queue_limit(queue_packets);
    vnet_observe(observe);
    if (scp_init(16)) return 2;
    if (transfer_init(&b, &tb, 2, 1, 0, total, cc) ||
        transfer_init(&a, &ta, 1, 2, 1, total, cc)) return 2;
    if (cc == SCP_CC_PROB &&
        (scp_prob_configure(a.stream, 100, 8000) || scp_prob_configure(b.stream, 100, 8000))) return 2;
    vnet_attach(&a, &b);
    sender = &a;
    timeout_handler[0] = a.stream->cc_on_timeout;
    timeout_handler[1] = b.stream->cc_on_timeout;
    a.stream->cc_on_timeout = count_timeout;
    b.stream->cc_on_timeout = count_timeout;
    a.send_backlog_bytes = backlog;
    printf("Generated transfer: %" PRIu64 " bytes, %s, %u Mbps/direction, %u ms one-way, %u ppm loss, seed %u\n",
        total, rate ? "serialized link" : "fixed delay without serialization", rate, delay, loss, seed);
    while (steps < (uint64_t)timeout * SCP_TICK_SCALE) {
        ++steps;
        clock_tick = steps;
        if (vnet_step() || transfer_step(&a) || transfer_step(&b)) break;
        check_sequences();
        if (!sequence_ok) break;
        if (a.done && b.done) { ok = 1; break; }
    }
    sent = a.sent; received = b.received; hash_a = a.hash; hash_b = b.hash;
    checksum_ok = sent == total && received == total && hash_a == hash_b && a.peer_hash == hash_a;
    if (ok && (wire_bytes[0] != total || wire_bytes[1] != sizeof(a.reply))) sequence_ok = 0;
    transfer_cleanup(&a); transfer_cleanup(&b);
    cleanup_ok = !core_live && !scp_timer_tree.count && list_empty(&scp_stream_queue);
    ok = ok && checksum_ok && sequence_ok && cleanup_ok;
    srtt_avg = srtt_samples ? (double)srtt_sum / srtt_samples / SCP_TICK_SCALE : 0;
    if (peak_count && delay) {
        for (peak_index = 0; peak_index < peak_count; ++peak_index) {
            if ((uint64_t)peaks[peak_index].cwnd * 5 >= (uint64_t)peaks[peak_count - 1].cwnd * 4) {
                startup = (double)(peaks[peak_index].tick - first_data_tick) / SCP_TICK_SCALE / (2.0 * delay);
                break;
            }
        }
    }
    fprintf(summary, "{\"success\":%s,\"requested_bytes\":%" PRIu64 ",\"accepted_bytes\":%" PRIu64 ",\"received_bytes\":%" PRIu64 ",\"checksum_algorithm\":\"FNV-1a-64\",\"checksum_a\":\"%016" PRIx64 "\",\"checksum_b\":\"%016" PRIx64 "\",\"simulated_ms\":%.3f,\"simulated_ticks\":%" PRIu64 ",\"tick_scale\":%u,\"cc\":%d,\"mbps\":%u,\"delay_ms\":%u,\"loss_ppm\":%u,\"seed\":%u,\"transport\":\"%s\",\"send_backlog_bytes\":%u,\"packets\":%" PRIu64 ",\"dropped\":%" PRIu64, ok ? "true" : "false", total, sent, received, hash_a, hash_b, (double)steps / SCP_TICK_SCALE, steps, SCP_TICK_SCALE, cc, rate, delay, loss, seed, rate ? "serialized_link" : "fixed_delay", backlog, vnet_packets, vnet_dropped);
    fprintf(summary, ",\"queue_packets\":%u,\"queue_dropped\":%" PRIu64
        ",\"checksum_ok\":%s,\"sequence_ok\":%s,\"cleanup_ok\":%s"
        ",\"core_live_allocations\":%" PRId64 ",\"active_timers\":%u"
        ",\"timeouts\":%" PRIu64 ",\"retrans_packets\":%" PRIu64
        ",\"timeouts_a\":%" PRIu64 ",\"timeouts_b\":%" PRIu64
        ",\"spurious_rexmit_ratio\":%.6f,\"srtt_max_ms\":%.6f,\"srtt_avg_ms\":%.6f"
        ",\"bufferbloat_ratio\":%.6f,\"startup_rtts_to_80pct\":%.6f}\n",
        queue_packets, vnet_queue_dropped, checksum_ok ? "true" : "false",
        sequence_ok ? "true" : "false", cleanup_ok ? "true" : "false",
        core_live, scp_timer_tree.count, timeouts[0] + timeouts[1], retrans_packets,
        timeouts[0], timeouts[1],
        (double)retrans_packets / (vnet_dropped ? vnet_dropped : 1),
        (double)srtt_max / SCP_TICK_SCALE, srtt_avg,
        delay ? srtt_avg / (2.0 * delay) : -1, startup);
    free(peaks);
    printf("%s: received=%" PRIu64 " A=%016" PRIx64 " B=%016" PRIx64 " virtual_ms=%.3f dropped=%" PRIu64 "\n", ok ? "PASS" : "FAIL", received, hash_a, hash_b, (double)steps / SCP_TICK_SCALE, vnet_dropped);
    fclose(summary);
    vnet_free();
    return ok ? 0 : 1;
}
