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
    uint32_t backlog = TRANSFER_SEND_BACKLOG_BYTES;
    struct vnet_endpoint ep_a = {0}, ep_b = {1};
    struct scp_transport_class ta = {.send = vnet_send, .user = &ep_a};
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
        else if (!strcmp(argv[i], "--send-backlog-bytes")) {
            if (value < MSS || value > UINT32_MAX) return 2;
            backlog = (uint32_t)value;
        }
        else return 2;
    }
    if (!total || loss > 1000000 || !timeout) return 2;
    summary = fopen("summary.json", "w");
    if (!summary) return 2;
    vnet_init(delay, rate, loss, seed);
    if (scp_init(16)) return 2;
    if (transfer_init(&b, &tb, 2, 1, 0, total, SCP_CC_AIMD) ||
        transfer_init(&a, &ta, 1, 2, 1, total, SCP_CC_AIMD)) return 2;
    vnet_attach(&a, &b);
    a.send_backlog_bytes = backlog;
    printf("Generated transfer: %" PRIu64 " bytes, %s, %u Mbps/direction, %u ms one-way, %u ppm loss, seed %u\n",
        total, rate ? "serialized link" : "fixed delay without serialization", rate, delay, loss, seed);
    while (scp_now_time() < timeout) {
        if (vnet_step() || transfer_step(&a) || transfer_step(&b)) break;
        if (a.done && b.done) { ok = 1; break; }
    }
    sent = a.sent; received = b.received; hash_a = a.hash; hash_b = b.hash;
    transfer_cleanup(&a); transfer_cleanup(&b);
    fprintf(summary, "{\"success\":%s,\"requested_bytes\":%" PRIu64 ",\"accepted_bytes\":%" PRIu64 ",\"received_bytes\":%" PRIu64 ",\"checksum_algorithm\":\"FNV-1a-64\",\"checksum_a\":\"%016" PRIx64 "\",\"checksum_b\":\"%016" PRIx64 "\",\"simulated_ms\":%u,\"mbps\":%u,\"delay_ms\":%u,\"loss_ppm\":%u,\"seed\":%u,\"transport\":\"%s\",\"send_backlog_bytes\":%u,\"packets\":%" PRIu64 ",\"dropped\":%" PRIu64 "}\n", ok ? "true" : "false", total, sent, received, hash_a, hash_b, scp_now_time(), rate, delay, loss, seed, rate ? "serialized_link" : "fixed_delay", backlog, vnet_packets, vnet_dropped);
    printf("%s: received=%" PRIu64 " A=%016" PRIx64 " B=%016" PRIx64 " virtual_ms=%u dropped=%" PRIu64 "\n", ok ? "PASS" : "FAIL", received, hash_a, hash_b, scp_now_time(), vnet_dropped);
    fclose(summary);
    vnet_free();
    return ok ? 0 : 1;
}
