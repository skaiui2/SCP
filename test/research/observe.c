#include "scp.h"
#include "observe.h"
#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>

#define RTT_MS 100
struct bin {
    uint32_t slots[RTT_MS], first_tick, last_tick, first_seq, last_seq;
    uint64_t count, bytes;
};
static struct bin bins[3];
static FILE *output;
static FILE *events;
static uint32_t bin_index, base_seq;
uint32_t research_first_data_tick = UINT32_MAX;
struct scp_stream *research_sender;
static void record_send(const void *buf, size_t len, uint32_t now);
uint32_t research_last_data_tick, research_new_bytes;
uint64_t research_new_packets, research_retrans_packets, research_retrans_bytes;
unsigned research_errors;
uint32_t research_feedback_min_ticks = UINT32_MAX, research_feedback_max_ticks;
uint64_t research_feedback_count;
uint64_t research_payload_packets[MSS + 1], research_nonfinal_short_packets;
uint32_t research_last_new_payload_bytes;

void research_observe(unsigned direction, int arrival, const void *buf, size_t len, uint32_t now)
{
    struct scp_hdr hdr;
    uint32_t echo;
    struct scp_stream *ss = research_sender && !scp_is_closed(1) ? research_sender : NULL;
    if (len < sizeof(hdr)) { ++research_errors; return; }
    memcpy(&hdr, buf, sizeof(hdr));
    if (!events) {
        events = fopen("network_events.csv", "w");
        if (!events) { ++research_errors; return; }
        fprintf(events, "event,direction,t_ms,t_tick,fd,flags,seq,ack,payload_bytes,echo_send_ms,echo_send_tick,cwnd_bytes,cwnd_cnt,flight_bytes,planned_q32_tick,gain_q16,next_slot_q32_tick\n");
    }
    if (arrival == VNET_SEND && direction == 0 && (hdr.flags & SCP_FLAG_DATA))
        record_send(buf, len, now);
    echo = ntohl(hdr.time_val);
    fprintf(events, "%s,%u,%.3f,%u,%u,%u,%u,%u,%u,%.3f,%u", arrival == 2 ? "input_returned" : arrival ? "arrival" : "send", direction,
        (double)now / SCP_TICK_SCALE, now, hdr.fd, hdr.flags, ntohl(hdr.seq), ntohl(hdr.ack), ntohs(hdr.len),
        (double)echo / SCP_TICK_SCALE, echo);
    if (ss) fprintf(events, ",%u,%u,%u,", ss->cwnd, ss->cwnd_cnt, ss->snd_nxt - ss->snd_una);
    else fprintf(events, ",,,,");
    if (ss && arrival == VNET_SEND && direction == 0 && (hdr.flags & SCP_FLAG_DATA) &&
        ss->pacing_next && ss->pacing_next->seq == ntohl(hdr.seq))
        fprintf(events, "%" PRIu64, ss->pacing_next->sent_time);
    if (ss) fprintf(events, ",%u,%" PRIu64, ss->cc_pacing_gain(ss), ss->pacing_next_time);
    else fprintf(events, ",,");
    fputc('\n', events);
    if (arrival == 1 && direction == 1 && hdr.flags == SCP_FLAG_ACK &&
        research_first_data_tick != UINT32_MAX && echo >= research_first_data_tick) {
        uint32_t elapsed = now - echo;
        ++research_feedback_count;
        if (elapsed < research_feedback_min_ticks) research_feedback_min_ticks = elapsed;
        if (elapsed > research_feedback_max_ticks) research_feedback_max_ticks = elapsed;
    }
}

static int flush_bin(void)
{
    char *names[] = {"all", "new", "retrans"};
    unsigned scope, slot;
    if (!output) return -1;
    for (scope = 0; scope < 3; ++scope) {
        struct bin *b = &bins[scope];
        fprintf(output, "%u,%s,%" PRIu64 ",%" PRIu64 ",", bin_index, names[scope], b->count, b->bytes);
        if (b->count)
            fprintf(output, "%.3f,%.3f,%u,%u,", (double)b->first_tick / SCP_TICK_SCALE,
                (double)b->last_tick / SCP_TICK_SCALE, b->first_seq, b->last_seq);
        else
            fprintf(output, ",,,,");
        fprintf(output, "\"[");
        for (slot = 0; slot < RTT_MS; ++slot) {
            if (slot) fputc(',', output);
            fprintf(output, "%u", b->slots[slot]);
        }
        fprintf(output, "]\"\n");
    }
    memset(bins, 0, sizeof(bins));
    ++bin_index;
    return ferror(output) ? -1 : 0;
}

static void add_packet(unsigned scope, uint32_t now, uint32_t seq, uint32_t bytes)
{
    struct bin *b = &bins[scope];
    if (!b->count) { b->first_tick = now; b->first_seq = seq; }
    b->last_tick = now; b->last_seq = seq;
    ++b->count; b->bytes += bytes;
    ++b->slots[((now - research_first_data_tick) / SCP_TICK_SCALE) % RTT_MS];
}

static void record_send(const void *buf, size_t len, uint32_t now)
{
    struct scp_hdr hdr;
    uint32_t seq, offset, bytes, target;
    unsigned scope;
    memcpy(&hdr, buf, sizeof(hdr));
    seq = ntohl(hdr.seq); bytes = ntohs(hdr.len);
    if (!bytes || bytes > MSS || bytes + sizeof(hdr) != len) { ++research_errors; return; }
    ++research_payload_packets[bytes];
    if (research_first_data_tick == UINT32_MAX) {
        research_first_data_tick = now; base_seq = seq;
        output = fopen("packet_rtt.csv", "w");
        if (!output) { ++research_errors; return; }
        fprintf(output, "rtt_index,scope,packet_count,payload_bytes,first_tx_ms,last_tx_ms,first_seq,last_seq,slot_counts\n");
    }
    target = (now - research_first_data_tick) / (RTT_MS * SCP_TICK_SCALE);
    while (bin_index < target) if (flush_bin()) { ++research_errors; return; }
    offset = seq - base_seq;
    if (offset == research_new_bytes) {
        if (research_new_packets && research_last_new_payload_bytes != MSS)
            ++research_nonfinal_short_packets;
        research_last_new_payload_bytes = bytes;
        scope = 1; research_new_bytes += bytes; ++research_new_packets;
    } else if (offset < research_new_bytes && bytes <= research_new_bytes - offset) {
        scope = 2; ++research_retrans_packets; research_retrans_bytes += bytes;
    } else { ++research_errors; return; }
    add_packet(0, now, seq, bytes); add_packet(scope, now, seq, bytes);
    research_last_data_tick = now;
}

int research_finish(uint32_t end_tick)
{
    int result = 0;
    if (events && fclose(events)) result = -1;
    events = NULL;
    if (!output) return result;
    while (research_first_data_tick + bin_index * RTT_MS * SCP_TICK_SCALE < end_tick)
        if (flush_bin()) { result = -1; break; }
    if (fclose(output)) result = -1;
    output = NULL;
    return result;
}
