#include "scp.h"
#include "vnet.h"
#include "../common/transfer.h"
#include <stdlib.h>
#include <string.h>
struct packet { struct packet *next; uint64_t due; size_t len; int dropped; unsigned char data[MTU]; };
static struct packet *head[2], *tail[2];
static uint64_t next_us[2];
static struct packet *waiting[2];
static uint32_t queued[2], queue_limit;
static uint64_t now;
static uint32_t delay, rate, loss, random_state;
static struct transfer *apps[2];
static vnet_observer observer;
void vnet_observe(vnet_observer callback) { observer = callback; }
void vnet_attach(struct transfer *a, struct transfer *b) { apps[0] = b; apps[1] = a; }
uint64_t vnet_packets, vnet_dropped;
uint64_t vnet_queue_dropped, vnet_pending;
void vnet_queue_limit(uint32_t packets) { queue_limit = packets; }
uint32_t scp_now_time(void) { return (uint32_t)now; }
void vnet_init(uint32_t delay_ms, uint32_t mbps, uint32_t loss_ppm, uint32_t seed)
{
    vnet_free();
    now = 0; next_us[0] = next_us[1] = 0;
    vnet_packets = vnet_dropped = 0;
    vnet_queue_dropped = 0;
    queue_limit = 0;
    delay = delay_ms; rate = mbps; loss = loss_ppm; random_state = seed ? seed : 1;
}
int vnet_send(void *user, const void *buf, size_t len)
{
    unsigned d = ((struct vnet_endpoint *)user)->direction;
    uint64_t start = (uint64_t)now * 1000 / SCP_TICK_SCALE;
    struct packet *p;
    int dropped;
    if (len > MTU) return -1;
    ++vnet_packets;
    if (observer) observer(d, VNET_SEND, buf, len, (uint32_t)now);
    while (waiting[d] && waiting[d]->due - (uint64_t)delay * 1000 <= start) {
        waiting[d] = waiting[d]->next;
        --queued[d];
    }
    if (queue_limit && queued[d] >= queue_limit) {
        ++vnet_dropped;
        ++vnet_queue_dropped;
        return (int)len;
    }
    if (rate) {
        if (start < next_us[d]) start = next_us[d];
        next_us[d] = start + (len * 8 + rate - 1) / rate;
        start = next_us[d];
    }
    random_state ^= random_state << 13;
    random_state ^= random_state >> 17;
    random_state ^= random_state << 5;
    dropped = random_state % 1000000 < loss;
    if (dropped) {
        ++vnet_dropped;
        if (!queue_limit) return (int)len;
    }
    p = malloc(sizeof(*p));
    if (!p) abort();
    p->next = NULL; p->due = start + (uint64_t)delay * 1000; p->len = len;
    p->dropped = dropped;
    memcpy(p->data, buf, len);
    if (tail[d]) tail[d]->next = p; else head[d] = p;
    tail[d] = p;
    if (!waiting[d]) waiting[d] = p;
    ++queued[d];
    ++vnet_pending;
    return (int)len;
}
int vnet_step(void)
{
    unsigned d;
    ++now;
    for (d = 0; d < 2; ++d) {
        while (head[d] && head[d]->due <= (uint64_t)now * 1000 / SCP_TICK_SCALE) {
            struct packet *p = head[d];
            int result;
            head[d] = p->next;
            if (!head[d]) tail[d] = NULL;
            if (waiting[d] == p) { waiting[d] = p->next; --queued[d]; }
            --vnet_pending;
            if (p->dropped) { free(p); continue; }
            if (observer) observer(d, VNET_ARRIVAL, p->data, p->len, (uint32_t)now);
            {
                const struct scp_hdr *hdr = (const struct scp_hdr *)p->data;
                int fd = (int)hdr->fd;
                result = apps[d] ? transfer_input(apps[d], p->data, p->len) : scp_input(NULL, p->data, p->len);
                if (result < 0 && scp_is_closed(fd)) result = 0;
                if (result >= 0 && observer)
                    observer(d, VNET_INPUT_RETURNED, p->data, p->len, (uint32_t)now);
            }
            free(p);
            if (result < 0) return -1;
        }
    }
    scp_timer_process();
    return 0;
}
void vnet_free(void)
{
    unsigned d;
    for (d = 0; d < 2; ++d) while (head[d]) {
        struct packet *p = head[d]; head[d] = p->next; free(p);
    }
    tail[0] = tail[1] = NULL;
    waiting[0] = waiting[1] = NULL;
    queued[0] = queued[1] = 0;
    vnet_pending = 0;
    apps[0] = apps[1] = NULL;
    observer = NULL;
}
