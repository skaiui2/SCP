#include "scp.h"
#include "vnet.h"
#include "../common/transfer.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
extern uint32_t scp_now_time(void);
int main(int argc, char **argv)
{
    uint64_t total = UINT64_C(1073741824), sent = 0, received = 0;
    uint64_t hash_a = UINT64_C(14695981039346656037), hash_b = hash_a;
    uint32_t delay = 10, rate = 100, loss = 0, seed = 1, timeout = 36000000;
    struct vnet_endpoint ep_a = {0}, ep_b = {1};
    struct scp_transport_class ta = {.send = vnet_send, .user = &ep_a};
    struct scp_transport_class tb = {.send = vnet_send, .user = &ep_b};
    struct transfer a, b;
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
    if (transfer_init(&b, &tb, 2, 1, 0, total, SCP_CC_AIMD) ||
        transfer_init(&a, &ta, 1, 2, 1, total, SCP_CC_AIMD)) return 2;
    vnet_attach(&a, &b);
    printf("Generated transfer: %" PRIu64 " bytes, %u Mbps/direction, %u ms one-way, %u ppm loss, seed %u\n", total, rate, delay, loss, seed);
    while (scp_now_time() < timeout) {
        if (vnet_step() || transfer_step(&a) || transfer_step(&b)) break;
        if (scp_now_time() % 100 == 0) {
            fprintf(samples, "%u,%" PRIu64 ",%" PRIu64 ",%u,%u,%u,%u,%" PRIu64 ",%" PRIu64 "\n", scp_now_time(), a.sent, b.received, a.cwnd, a.flight, a.reserved, a.srtt, vnet_packets, vnet_dropped);
        }
        if (a.done && b.done) { ok = 1; break; }
    }
    sent = a.sent; received = b.received; hash_a = a.hash; hash_b = b.hash;
    transfer_cleanup(&a); transfer_cleanup(&b);
    fprintf(summary, "{\"success\":%s,\"requested_bytes\":%" PRIu64 ",\"accepted_bytes\":%" PRIu64 ",\"received_bytes\":%" PRIu64 ",\"checksum_algorithm\":\"FNV-1a-64\",\"checksum_a\":\"%016" PRIx64 "\",\"checksum_b\":\"%016" PRIx64 "\",\"simulated_ms\":%u,\"mbps\":%u,\"delay_ms\":%u,\"loss_ppm\":%u,\"seed\":%u,\"packets\":%" PRIu64 ",\"dropped\":%" PRIu64 "}\n", ok ? "true" : "false", total, sent, received, hash_a, hash_b, scp_now_time(), rate, delay, loss, seed, vnet_packets, vnet_dropped);
    printf("%s: received=%" PRIu64 " A=%016" PRIx64 " B=%016" PRIx64 " virtual_ms=%u dropped=%" PRIu64 "\n", ok ? "PASS" : "FAIL", received, hash_a, hash_b, scp_now_time(), vnet_dropped);
    fclose(samples); fclose(summary);
    vnet_free();
    return ok ? 0 : 1;
}
