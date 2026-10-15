#include "scp.h"
#include "research.h"
#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>

#define RTT_MS 100
struct bin {
    uint32_t slots[RTT_MS], first_ms, last_ms, first_seq, last_seq;
    uint64_t count, bytes;
};
static struct bin bins[3];
static FILE *output;
static FILE *events;
static uint32_t bin_index, base_seq;
extern uint32_t scp_now_time(void), scp_experiment_first_data_ms;
uint32_t research_last_data_ms, research_new_bytes;
uint64_t research_new_packets, research_retrans_packets, research_retrans_bytes;
unsigned research_errors;
uint32_t research_feedback_min_ms = UINT32_MAX, research_feedback_max_ms;
uint64_t research_feedback_count;
uint64_t research_payload_packets[MSS + 1], research_nonfinal_short_packets;
uint32_t research_last_new_payload_bytes;

void research_observe(unsigned direction, int arrival, const void *buf, size_t len, uint32_t now)
{
    struct scp_hdr hdr;
    uint32_t echo;
    if (len < sizeof(hdr)) { ++research_errors; return; }
    memcpy(&hdr, buf, sizeof(hdr));
    if (!events) {
        events = fopen("network_events.csv", "w");
        if (!events) { ++research_errors; return; }
        fprintf(events, "event,direction,t_ms,fd,flags,seq,ack,payload_bytes,echo_send_ms\n");
    }
    echo = ntohl(hdr.time_val);
    fprintf(events, "%s,%u,%u,%u,%u,%u,%u,%u,%u\n", arrival == 2 ? "input_returned" : arrival ? "arrival" : "send", direction,
        now, hdr.fd, hdr.flags, ntohl(hdr.seq), ntohl(hdr.ack), ntohs(hdr.len), echo);
    if (arrival == 1 && direction == 1 && hdr.flags == SCP_FLAG_ACK &&
        scp_experiment_first_data_ms != UINT32_MAX && echo >= scp_experiment_first_data_ms) {
        uint32_t elapsed = now - echo;
        ++research_feedback_count;
        if (elapsed < research_feedback_min_ms) research_feedback_min_ms = elapsed;
        if (elapsed > research_feedback_max_ms) research_feedback_max_ms = elapsed;
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
            fprintf(output, "%u,%u,%u,%u,", b->first_ms, b->last_ms, b->first_seq, b->last_seq);
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
    if (!b->count) { b->first_ms = now; b->first_seq = seq; }
    b->last_ms = now; b->last_seq = seq;
    ++b->count; b->bytes += bytes;
    ++b->slots[(now - scp_experiment_first_data_ms) % RTT_MS];
}

int research_send(void *user, const void *buf, size_t len)
{
    struct scp_hdr hdr;
    uint32_t now = scp_now_time(), seq, offset, bytes, target;
    unsigned scope;
    memcpy(&hdr, buf, sizeof(hdr));
    if (((struct vnet_endpoint *)user)->direction != 0 || !(hdr.flags & SCP_FLAG_DATA))
        return vnet_send(user, buf, len);
    seq = ntohl(hdr.seq); bytes = ntohs(hdr.len);
    if (!bytes || bytes > MSS || bytes + sizeof(hdr) != len) { ++research_errors; return -1; }
    ++research_payload_packets[bytes];
    if (scp_experiment_first_data_ms == UINT32_MAX) {
        scp_experiment_first_data_ms = now; base_seq = seq;
        output = fopen("packet_rtt.csv", "w");
        if (!output) { ++research_errors; return -1; }
        fprintf(output, "rtt_index,scope,packet_count,payload_bytes,first_tx_ms,last_tx_ms,first_seq,last_seq,slot_counts\n");
    }
    target = (now - scp_experiment_first_data_ms) / RTT_MS;
    while (bin_index < target) if (flush_bin()) { ++research_errors; return -1; }
    offset = seq - base_seq;
    if (offset == research_new_bytes) {
        if (research_new_packets && research_last_new_payload_bytes != MSS)
            ++research_nonfinal_short_packets;
        research_last_new_payload_bytes = bytes;
        scope = 1; research_new_bytes += bytes; ++research_new_packets;
    } else if (offset < research_new_bytes && bytes <= research_new_bytes - offset) {
        scope = 2; ++research_retrans_packets; research_retrans_bytes += bytes;
    } else { ++research_errors; return -1; }
    add_packet(0, now, seq, bytes); add_packet(scope, now, seq, bytes);
    research_last_data_ms = now;
    return vnet_send(user, buf, len);
}

int research_finish(uint32_t end_ms)
{
    int result = 0;
    if (events && fclose(events)) result = -1;
    events = NULL;
    if (!output) return result;
    while (scp_experiment_first_data_ms + bin_index * RTT_MS < end_ms)
        if (flush_bin()) { result = -1; break; }
    if (fclose(output)) result = -1;
    output = NULL;
    return result;
}
