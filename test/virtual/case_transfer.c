#include "scp.h"
#include "vnet.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
extern uint32_t scp_now_time(void);
static uint64_t checksum(uint64_t hash, const unsigned char *buf, size_t len)
{
    size_t i;
    for (i = 0; i < len; ++i) { hash ^= buf[i]; hash *= UINT64_C(1099511628211); }
    return hash;
}
static unsigned char byte_at(uint64_t offset)
{
    uint64_t x = offset / 8 + UINT64_C(0x9e3779b97f4a7c15);
    x = (x ^ (x >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)) * UINT64_C(0x94d049bb133111eb);
    x ^= x >> 31;
    return (unsigned char)(x >> ((offset % 8) * 8));
}
int main(int argc, char **argv)
{
    uint64_t total = UINT64_C(1073741824), sent = 0, received = 0;
    uint64_t hash_a = UINT64_C(14695981039346656037), hash_b = hash_a;
    uint32_t delay = 10, rate = 100, loss = 0, seed = 1, timeout = 36000000;
    unsigned char sendbuf[65536], recvbuf[65536];
    struct vnet_endpoint ep_a = {0}, ep_b = {1};
    struct scp_transport_class ta = {.send = vnet_send, .user = &ep_a};
    struct scp_transport_class tb = {.send = vnet_send, .user = &ep_b};
    struct scp_stream *a, *b;
    FILE *samples, *summary;
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
        else return 2;
    }
    if (!total || !rate || loss > 1000000 || !timeout) return 2;
    samples = fopen("samples.csv", "w");
    summary = fopen("summary.json", "w");
    if (!samples || !summary) return 2;
    fprintf(samples, "t_ms,accepted_bytes,received_bytes,cwnd,flight,reserved,srtt,packets,dropped\n");
    vnet_init(delay, rate, loss, seed);
    if (scp_init(16)) return 2;
    a = scp_stream_alloc(&ta, 1, 2); b = scp_stream_alloc(&tb, 2, 1);
    if (!a || !b || scp_set_cc(1, SCP_CC_AIMD) || scp_set_cc(2, SCP_CC_AIMD)) return 2;
    if (scp_connect(1)) return 2;
    printf("Generated transfer: %" PRIu64 " bytes, %u Mbps/direction, %u ms one-way, %u ppm loss, seed %u\n", total, rate, delay, loss, seed);
    while (scp_now_time() < timeout) {
        int n;
        size_t j;
        if (vnet_step()) break;
        if (a->state == SCP_ESTABLISHED && b->state == SCP_ESTABLISHED) {
            while (sent < total && sent - received < 1048576) {
                size_t count = (size_t)min(total - sent, sizeof(sendbuf));
                int result;
                for (j = 0; j < count; ++j) sendbuf[j] = byte_at(sent + j);
                result = scp_send(1, sendbuf, count);
                if (result == SCP_ERR_NOBUF) break;
                if (result) goto done;
                hash_a = checksum(hash_a, sendbuf, count); sent += count;
            }
            while ((n = scp_recv(2, recvbuf, sizeof(recvbuf))) > 0) {
                if (received + (unsigned)n > total) goto done;
                for (j = 0; j < (size_t)n; ++j) if (recvbuf[j] != byte_at(received + j)) {
                    fprintf(stderr, "Content mismatch at byte %" PRIu64 "\n", received + j); goto done;
                }
                hash_b = checksum(hash_b, recvbuf, (size_t)n); received += (unsigned)n;
            }
        }
        if (scp_now_time() % 100 == 0 || received == total) {
            uint32_t reserved = (a->schedule_next ? a->schedule_next->seq : a->snd_seq_q) - a->snd_una;
            fprintf(samples, "%u,%" PRIu64 ",%" PRIu64 ",%u,%u,%u,%u,%" PRIu64 ",%" PRIu64 "\n", scp_now_time(), sent, received, a->cwnd, a->snd_nxt - a->snd_una, reserved, a->srtt, vnet_packets, vnet_dropped);
        }
        if (sent == total && received == total && a->snd_una == a->snd_seq_q) { ok = hash_a == hash_b; break; }
    }
done:
    if (ok) {
        scp_close(1);
        for (i = 0; i < 10000 && (!scp_is_closed(1) || !scp_is_closed(2)); ++i) if (vnet_step()) break;
        if (!scp_is_closed(1) || !scp_is_closed(2)) {
            fprintf(stderr, "Close phase exceeded 10000 virtual ms: A_closed=%d B_closed=%d; data already verified\n", scp_is_closed(1), scp_is_closed(2));
            ok = 0;
        }
    }
    fprintf(summary, "{\"success\":%s,\"requested_bytes\":%" PRIu64 ",\"accepted_bytes\":%" PRIu64 ",\"received_bytes\":%" PRIu64 ",\"checksum_algorithm\":\"FNV-1a-64\",\"checksum_a\":\"%016" PRIx64 "\",\"checksum_b\":\"%016" PRIx64 "\",\"simulated_ms\":%u,\"mbps\":%u,\"delay_ms\":%u,\"loss_ppm\":%u,\"seed\":%u,\"packets\":%" PRIu64 ",\"dropped\":%" PRIu64 "}\n", ok ? "true" : "false", total, sent, received, hash_a, hash_b, scp_now_time(), rate, delay, loss, seed, vnet_packets, vnet_dropped);
    printf("%s: received=%" PRIu64 " A=%016" PRIx64 " B=%016" PRIx64 " virtual_ms=%u dropped=%" PRIu64 "\n", ok ? "PASS" : "FAIL", received, hash_a, hash_b, scp_now_time(), vnet_dropped);
    fclose(samples); fclose(summary);
    vnet_free();
    return ok ? 0 : 1;
}
