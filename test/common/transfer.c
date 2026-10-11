#include "transfer.h"
#include <stdio.h>
#include <string.h>
#include <inttypes.h>
static unsigned char byte_at(uint64_t offset)
{
    uint64_t x = offset / 8 + UINT64_C(0x9e3779b97f4a7c15);
    x = (x ^ (x >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)) * UINT64_C(0x94d049bb133111eb);
    x ^= x >> 31;
    return (unsigned char)(x >> ((offset % 8) * 8));
}
static uint64_t checksum(uint64_t hash, const unsigned char *buf, size_t len)
{
    size_t i;
    for (i = 0; i < len; ++i) { hash ^= buf[i]; hash *= UINT64_C(1099511628211); }
    return hash;
}
int transfer_init(struct transfer *t, struct scp_transport_class *transport,
                  int local_fd, int remote_fd, int sender, uint64_t bytes, int cc)
{
    memset(t, 0, sizeof(*t));
    t->fd = local_fd; t->sender = sender; t->total = bytes;
    t->hash = UINT64_C(14695981039346656037);
    t->stream = scp_stream_alloc(transport, local_fd, remote_fd);
    if (!t->stream || scp_set_cc(local_fd, cc)) return -1;
    if (sender) {
        t->started = 1;
        if (scp_connect(local_fd)) return -1;
    }
    printf("[%c][SCP] waiting for ESTABLISHED; bytes=%" PRIu64 "\n", sender ? 'A' : 'B', bytes);
    return 0;
}
int transfer_input(struct transfer *t, void *buf, size_t len)
{
    const struct scp_hdr *hdr = buf;
    int result;
    if (t->started && scp_is_closed(t->fd)) return 0;
    result = scp_input(NULL, buf, len);
    if (!result && len >= sizeof(*hdr) && (hdr->flags & SCP_FLAG_CONNECT)) t->started = 1;
    if (result < 0 && t->started && scp_is_closed(t->fd)) return 0;
    return result;
}
int transfer_step(struct transfer *t)
{
    unsigned char buf[65536];
    int n;
    size_t i;
    if (t->done || t->failed) return t->failed ? -1 : 0;
    if (t->started && scp_is_closed(t->fd)) {
        t->done = t->sender ? t->close_requested && t->sent == t->total && t->reply_size == 16 && t->hash == t->peer_hash
                            : t->received == t->total && t->checksum_sent;
        if (!t->done) goto fail;
        printf("[%c][SCP] PASS bytes=%" PRIu64 " checksum=%016" PRIx64 "\n", t->sender ? 'A' : 'B', t->total, t->hash);
        return 0;
    }
    t->cwnd = t->stream->cwnd;
    t->flight = t->stream->snd_nxt - t->stream->snd_una;
    t->reserved = (t->stream->schedule_next ? t->stream->schedule_next->seq : t->stream->snd_seq_q) - t->stream->snd_una;
    t->srtt = t->stream->srtt;
    if (t->stream->state != SCP_ESTABLISHED) return 0;
    if (!t->established) {
        t->established = 1;
        printf("[%c][SCP] established\n", t->sender ? 'A' : 'B');
    }
    if (t->sender) {
        while (t->sent < t->total && t->stream->snd_seq_q - t->stream->snd_una < 1048576) {
            size_t count = (size_t)min(t->total - t->sent, sizeof(buf));
            int result;
            if (t->file) {
                if (!t->pending_size) {
                    if (fread(t->pending, 1, count, t->file) != count) goto fail;
                    t->pending_size = count;
                }
                memcpy(buf, t->pending, t->pending_size);
                count = t->pending_size;
            } else {
                for (i = 0; i < count; ++i) buf[i] = byte_at(t->sent + i);
            }
            result = scp_send(t->fd, buf, count);
            if (result == SCP_ERR_NOBUF) break;
            if (result) goto fail;
            t->pending_size = 0;
            t->hash = checksum(t->hash, buf, count); t->sent += count;
        }
        while ((n = scp_recv(t->fd, buf, sizeof(buf))) > 0) {
            if (t->reply_size + (size_t)n > sizeof(t->reply)) goto fail;
            memcpy(t->reply + t->reply_size, buf, (size_t)n);
            t->reply_size += (size_t)n;
        }
        if (t->reply_size == 16 && !t->close_requested) {
            uint64_t bytes = 0;
            for (i = 0; i < 8; ++i) {
                bytes |= (uint64_t)t->reply[i] << (i * 8);
                t->peer_hash |= (uint64_t)t->reply[i + 8] << (i * 8);
            }
            if (bytes != t->total || t->hash != t->peer_hash || t->sent != t->total) goto fail;
            t->close_requested = 1;
            scp_close(t->fd);
        }
    } else {
        while ((n = scp_recv(t->fd, buf, sizeof(buf))) > 0) {
            if (t->received + (size_t)n > t->total) goto fail;
            if (t->file) {
                if (fwrite(buf, 1, (size_t)n, t->file) != (size_t)n) goto fail;
            } else {
                for (i = 0; i < (size_t)n; ++i) if (buf[i] != byte_at(t->received + i)) goto fail;
            }
            t->hash = checksum(t->hash, buf, (size_t)n); t->received += (size_t)n;
        }
        if (t->received == t->total && !t->checksum_sent) {
            int result;
            if (t->file && fflush(t->file)) goto fail;
            for (i = 0; i < 8; ++i) {
                t->reply[i] = (unsigned char)(t->total >> (i * 8));
                t->reply[i + 8] = (unsigned char)(t->hash >> (i * 8));
            }
            result = scp_send(t->fd, t->reply, sizeof(t->reply));
            if (result == SCP_ERR_NOBUF) return 0;
            if (result) goto fail;
            t->checksum_sent = 1;
        }
    }
    return 0;
fail:
    t->failed = 1;
    fprintf(stderr, "[%c][SCP] failed: sent=%" PRIu64 " received=%" PRIu64 "\n", t->sender ? 'A' : 'B', t->sent, t->received);
    return -1;
}
void transfer_cleanup(struct transfer *t)
{
    if (t->file) { fclose(t->file); t->file = NULL; }
    if (t->stream && !t->done && !(t->started && scp_is_closed(t->fd))) scp_stream_free(t->stream);
}
