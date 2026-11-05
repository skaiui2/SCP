#include "sscp.h"
#include "vnet.h"
#include "queue.h"
#include "hashmap.h"
#include "../common/transfer.h"
#include <stdlib.h>
#include <string.h>

struct vnet_packet {
    struct list_node wire_node;
    struct list_node buffer_node;
    uint64_t due_time_us;
    size_t length;
    int is_dropped;
    unsigned char data[MTU];
};

struct vnet_channel {
    unsigned direction;
    struct list_node wire_queue;
    struct list_node buffer_queue;
    uint64_t next_tx_free_us;
    uint32_t queued_packets;
};

struct vnet_network {
    struct vnet_channel channels[2];
    uint64_t clock_tick;
    uint32_t delay_ms;
    uint32_t rate_mbps;
    uint32_t loss_ppm;
    uint32_t queue_limit;
    uint32_t prng_state;
    struct transfer *apps[2];
    vnet_observer observer;
};

static struct vnet_network g_vnet;
static int g_vnet_initialized;

uint64_t vnet_packets;
uint64_t vnet_dropped;
uint64_t vnet_queue_dropped;
uint64_t vnet_pending;

static uint32_t vnet_random_step(struct vnet_network *net)
{
    net->prng_state ^= net->prng_state << 13;
    net->prng_state ^= net->prng_state >> 17;
    net->prng_state ^= net->prng_state << 5;
    return net->prng_state;
}

static void channel_init(struct vnet_channel *ch, unsigned direction)
{
    ch->direction = direction;
    list_node_init(&ch->wire_queue);
    list_node_init(&ch->buffer_queue);
    ch->next_tx_free_us = 0;
    ch->queued_packets = 0;
}

static void channel_flush_buffer(struct vnet_channel *ch, uint64_t now_us, uint64_t delay_us)
{
    while (!list_empty(&ch->buffer_queue)) {
        struct list_node *node = ch->buffer_queue.next;
        struct vnet_packet *pkt = container_of(node, struct vnet_packet, buffer_node);

        if (pkt->due_time_us - delay_us > now_us) {
            break;
        }

        queue_dequeue(&ch->buffer_queue);
        list_node_init(&pkt->buffer_node);
        --ch->queued_packets;
    }
}

static uint64_t channel_calc_tx_finish(struct vnet_channel *ch, uint64_t now_us, size_t len, uint32_t rate_mbps)
{
    uint64_t tx_start_us;
    uint64_t tx_duration_us;

    tx_start_us = now_us;
    if (rate_mbps == 0) {
        return tx_start_us;
    }

    if (tx_start_us < ch->next_tx_free_us) {
        tx_start_us = ch->next_tx_free_us;
    }

    tx_duration_us = (len * 8 + rate_mbps - 1) / rate_mbps;
    ch->next_tx_free_us = tx_start_us + tx_duration_us;

    return ch->next_tx_free_us;
}

static void channel_enqueue(struct vnet_channel *ch, struct vnet_packet *pkt)
{
    queue_enqueue(&ch->wire_queue, &pkt->wire_node);
    queue_enqueue(&ch->buffer_queue, &pkt->buffer_node);
    ++ch->queued_packets;
    ++vnet_pending;
}

static struct vnet_packet *channel_pop_arrived(struct vnet_channel *ch, uint64_t now_us)
{
    struct list_node *node;
    struct vnet_packet *pkt;

    if (list_empty(&ch->wire_queue)) {
        return NULL;
    }

    node = ch->wire_queue.next;
    pkt = container_of(node, struct vnet_packet, wire_node);
    if (pkt->due_time_us > now_us) {
        return NULL;
    }

    queue_dequeue(&ch->wire_queue);

    if (!list_empty(&pkt->buffer_node)) {
        list_remove(&pkt->buffer_node);
        list_node_init(&pkt->buffer_node);
        --ch->queued_packets;
    }
    --vnet_pending;

    return pkt;
}

static void channel_clear(struct vnet_channel *ch)
{
    while (!list_empty(&ch->wire_queue)) {
        struct list_node *node = queue_dequeue(&ch->wire_queue);
        struct vnet_packet *pkt = container_of(node, struct vnet_packet, wire_node);
        free(pkt);
    }

    list_node_init(&ch->wire_queue);
    list_node_init(&ch->buffer_queue);
    ch->next_tx_free_us = 0;
    ch->queued_packets = 0;
}

uint32_t sscp_now_time(void)
{
    return (uint32_t)g_vnet.clock_tick;
}

void vnet_observe(vnet_observer callback)
{
    g_vnet.observer = callback;
}

void vnet_attach(struct transfer *a, struct transfer *b)
{
    g_vnet.apps[0] = b;
    g_vnet.apps[1] = a;
}

void vnet_queue_limit(uint32_t packets)
{
    g_vnet.queue_limit = packets;
}

void vnet_free(void)
{
    if (!g_vnet_initialized) {
        return;
    }
    channel_clear(&g_vnet.channels[0]);
    channel_clear(&g_vnet.channels[1]);
    vnet_pending = 0;
    g_vnet.apps[0] = NULL;
    g_vnet.apps[1] = NULL;
    g_vnet.observer = NULL;
}

void vnet_init(uint32_t delay_ms, uint32_t mbps, uint32_t loss_ppm, uint32_t seed)
{
    vnet_free();
    g_vnet_initialized = 1;

    channel_init(&g_vnet.channels[0], 0);
    channel_init(&g_vnet.channels[1], 1);

    g_vnet.clock_tick = 0;
    g_vnet.delay_ms = delay_ms;
    g_vnet.rate_mbps = mbps;
    g_vnet.loss_ppm = loss_ppm;
    g_vnet.queue_limit = 0;
    g_vnet.prng_state = seed ? seed : 1;

    vnet_packets = 0;
    vnet_dropped = 0;
    vnet_queue_dropped = 0;
}

int vnet_send(void *user, const void *buf, size_t len)
{
    struct vnet_endpoint *endpoint;
    struct vnet_channel *channel;
    struct vnet_packet *pkt;
    uint64_t now_us;
    uint64_t delay_us;
    uint64_t tx_finish_us;
    int is_random_drop;

    if (len > MTU) {
        return -1;
    }

    endpoint = (struct vnet_endpoint *)user;
    channel = &g_vnet.channels[endpoint->direction];
    now_us = g_vnet.clock_tick * 1000 / SSCP_TICK_SCALE;
    delay_us = (uint64_t)g_vnet.delay_ms * 1000;

    ++vnet_packets;
    if (g_vnet.observer) {
        g_vnet.observer(channel->direction, VNET_SEND, buf, len, (uint32_t)g_vnet.clock_tick);
    }

    channel_flush_buffer(channel, now_us, delay_us);

    if (g_vnet.queue_limit && channel->queued_packets >= g_vnet.queue_limit) {
        ++vnet_dropped;
        ++vnet_queue_dropped;
        return (int)len;
    }

    tx_finish_us = channel_calc_tx_finish(channel, now_us, len, g_vnet.rate_mbps);

    is_random_drop = (vnet_random_step(&g_vnet) % 1000000) < g_vnet.loss_ppm;
    if (is_random_drop) {
        ++vnet_dropped;
        if (!g_vnet.queue_limit) {
            return (int)len;
        }
    }

    pkt = malloc(sizeof(*pkt));
    if (!pkt) {
        abort();
    }

    list_node_init(&pkt->wire_node);
    list_node_init(&pkt->buffer_node);
    pkt->length = len;
    pkt->due_time_us = tx_finish_us + delay_us;
    pkt->is_dropped = is_random_drop;
    memcpy(pkt->data, buf, len);

    channel_enqueue(channel, pkt);
    return (int)len;
}

static int dispatch_packet(unsigned direction, struct vnet_packet *pkt)
{
    const struct sscp_hdr *hdr;
    int fd;
    int result;

    if (g_vnet.observer) {
        g_vnet.observer(direction, VNET_ARRIVAL, pkt->data, pkt->length, (uint32_t)g_vnet.clock_tick);
    }

    hdr = (const struct sscp_hdr *)pkt->data;
    fd = (int)hdr->fd;

    if (g_vnet.apps[direction]) {
        result = transfer_input(g_vnet.apps[direction], pkt->data, pkt->length);
    } else {
        result = sscp_input(NULL, pkt->data, pkt->length);
    }

    if (result < 0 && sscp_is_closed(fd)) {
        result = 0;
    }

    if (result >= 0 && g_vnet.observer) {
        g_vnet.observer(direction, VNET_INPUT_RETURNED, pkt->data, pkt->length, (uint32_t)g_vnet.clock_tick);
    }

    return result;
}

int vnet_step(void)
{
    uint64_t now_us;
    unsigned d;

    ++g_vnet.clock_tick;
    now_us = g_vnet.clock_tick * 1000 / SSCP_TICK_SCALE;

    for (d = 0; d < 2; ++d) {
        struct vnet_channel *channel = &g_vnet.channels[d];
        struct vnet_packet *pkt;

        while ((pkt = channel_pop_arrived(channel, now_us)) != NULL) {
            int result = 0;

            if (!pkt->is_dropped) {
                result = dispatch_packet(d, pkt);
            }

            free(pkt);
            if (result < 0) {
                return -1;
            }
        }
    }

    sscp_timer_process();
    return 0;
}
