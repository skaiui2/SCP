#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
static int live, allocations_left = -1;
static void *test_malloc(size_t size)
{
    void *p;
    if (!allocations_left) return NULL;
    if (allocations_left > 0) --allocations_left;
    p = malloc(size);
    if (p) ++live;
    return p;
}
static void test_free(void *p) { if (p) --live; free(p); }
#define malloc test_malloc
#define free test_free
#include "../../scp.c"
#undef malloc
#undef free
static uint32_t now;
uint32_t scp_now_time(void) { return now; }
static int sink(void *user, const void *buf, size_t len) { return (int)len; }
static struct scp_buf *receive_buffer(struct scp_stream *s)
{
    uint32_t total = sizeof(struct scp_buf) + sizeof(struct scp_hdr) + 1;
    struct scp_buf *p = scp_buf_alloc(total);
    assert(p);
    p->dir = SCP_DIR_INPUT; p->len = sizeof(struct scp_hdr) + 1;
    s->rcv_wmem += total;
    return p;
}
int main(void)
{
    struct scp_transport_class t = {.send = sink};
    struct scp_stream *s;
    struct scp_buf *p;
    struct scp_hdr header = {0};
    unsigned char data[3 * MSS];
    uint32_t seq, mem;
    int baseline;
    memset(data, 1, sizeof(data));
    assert(!scp_init(16));
    s = scp_stream_alloc(&t, 1, 2);
    assert(s && scp_is_closed(1));
    assert(!scp_stream_alloc(&t, 1, 3));
    assert(hashmap_get(&scp_stream_map, (void *)1) == s);
    assert(!scp_connect(1));
    for (now = 1; now < 13000; ++now) scp_timer_process();
    assert(scp_is_closed(1) && !hashmap_get(&scp_stream_map, (void *)1) && live == 0);
    s = scp_stream_alloc(&t, 1, 2); s->state = SCP_ESTABLISHED;
    assert(!scp_send(1, data, sizeof(data)));
    p = receive_buffer(s); queue_enqueue(&s->rcv_data_q, &p->list);
    p = receive_buffer(s); rb_node_init(&p->rb); p->rb.value = 1; rb_insert_node(&s->rcv_buf_q, &p->rb);
    scp_stream_free(s);
    assert(live == 0 && !scp_timer_tree.rb_node);
    s = scp_stream_alloc(&t, 1, 2); s->state = SCP_ESTABLISHED;
    seq = s->snd_seq_q; mem = s->snd_wmem; baseline = live;
    allocations_left = 1;
    assert(scp_send(1, data, sizeof(data)) == SCP_ERR_NOMEM);
    allocations_left = -1;
    assert(live == baseline && s->snd_seq_q == seq && s->snd_wmem == mem);
    assert(list_empty(&s->snd_q) && !s->schedule_next && !s->pacing_next);
    assert(scp_input(NULL, data, 1) == SCP_ERR_GENERIC);
    header.fd = 1; header.len = htons(1);
    assert(scp_input(NULL, &header, sizeof(header)) == SCP_ERR_GENERIC);
    header.len = 0; header.flags = SCP_FLAG_ACK;
    header.cksum = in_checksum(&header, sizeof(header));
    allocations_left = 0;
    assert(scp_input(NULL, &header, sizeof(header)) == SCP_ERR_NOMEM);
    allocations_left = -1;
    assert(s->rcv_wmem == 0);
    assert(!scp_send(1, data, sizeof(data)));
    scp_close(1);
    assert(s->state == SCP_ESTABLISHED && s->close_pending && s->t_fin.active);
    assert(scp_send(1, data, 1) == SCP_ERR_GENERIC);
    now += IDLE_TIMEOUT;
    scp_timer_process();
    assert(scp_is_closed(1) && live == 0 && !scp_timer_tree.rb_node);
    puts("PASS: cleanup, handshake timeout, duplicate fd, atomic send, input validation, close drain timeout");
    return 0;
}
