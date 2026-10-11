#ifndef SCP_TEST_TRANSFER_H
#define SCP_TEST_TRANSFER_H
#include "scp.h"
#include <stdio.h>
struct transfer {
    struct scp_stream *stream;
    uint64_t total, sent, received, hash, peer_hash;
    int fd, sender, started, established, checksum_sent, close_requested, done, failed;
    unsigned char reply[16];
    size_t reply_size;
    uint32_t cwnd, flight, reserved, srtt;
    FILE *file;
    unsigned char pending[65536];
    size_t pending_size;
};
int transfer_init(struct transfer *t, struct scp_transport_class *transport,
                  int local_fd, int remote_fd, int sender, uint64_t bytes, int cc);
int transfer_input(struct transfer *t, void *buf, size_t len);
int transfer_step(struct transfer *t);
void transfer_cleanup(struct transfer *t);
int transfer_udp_run(int sender, const char *peer_ip, uint64_t bytes,
                     int cc, int flows, uint32_t seconds, int mixed, const char *file_path);
#endif
