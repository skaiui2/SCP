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
#include "../common/transfer.h"
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
static void check_tree_order(struct rb_root *root)
{
    struct rb_node *node, *previous = NULL;
    unsigned count = 0;
    assert(rb_first(root) == root->first_node && rb_last(root) == root->last_node);
    for (node = rb_first(root); node; node = rb_next(node)) {
        assert(rb_prev(node) == previous);
        if (previous) assert(root->compare(previous, node) <= 0);
        previous = node;
        ++count;
    }
    assert(count == root->count && previous == root->last_node);
}
static int numeric_compare(struct rb_node *a, struct rb_node *b)
{
    uint32_t ka = container_of(a, struct scp_buf, rb)->seq;
    uint32_t kb = container_of(b, struct scp_buf, rb)->seq;
    return (ka > kb) - (ka < kb);
}
static void test_tree_order(void)
{
    uint32_t keys[] = {0x10, 0xfffffff0, 0, 0xffffffe0, 0x10};
    uint32_t expected[2][5] = {
        {0, 0x10, 0x10, 0xffffffe0, 0xfffffff0},
        {0xffffffe0, 0xfffffff0, 0, 0x10, 0x10}
    };
    struct scp_buf buffers[5] = {0}, query = {0};
    struct rb_root root;
    struct rb_node *node;
    unsigned i, mode, order;
    for (mode = 0; mode < 2; ++mode) for (order = 0; order < 2; ++order) {
        rb_root_init(&root, mode ? scp_receive_compare : numeric_compare);
        assert(!search_node(&root, &query.rb) && !rb_first_greater(&root, &query.rb));
        check_tree_order(&root);
        for (i = 0; i < 5; ++i) {
            unsigned index = order ? 4 - i : i;
            buffers[index].seq = keys[index];
            rb_node_init(&buffers[index].rb);
            rb_insert_node(&root, &buffers[index].rb);
            check_tree_order(&root);
        }
        node = rb_first(&root);
        for (i = 0; i < 5; ++i) {
            unsigned next = i + 1;
            assert(node && container_of(node, struct scp_buf, rb)->seq == expected[mode][i]);
            node = rb_next(node);
            query.seq = expected[mode][i];
            assert(container_of(search_node(&root, &query.rb), struct scp_buf, rb)->seq == query.seq);
            while (next < 5 && expected[mode][next] == query.seq) ++next;
            if (next == 5) assert(!rb_first_greater(&root, &query.rb));
            else assert(container_of(rb_first_greater(&root, &query.rb), struct scp_buf, rb)->seq == expected[mode][next]);
        }
        assert(!node);
        query.seq = 5; assert(!search_node(&root, &query.rb));
        for (i = 0; i < 5; ++i) {
            rb_remove_node(&root, &buffers[i].rb);
            check_tree_order(&root);
        }
    }
    puts("PASS: numeric/sequence ordering, equal keys, search, upper bound, cache and deletion");
}
static void test_receive_wrap(struct scp_transport_class *transport)
{
    unsigned order[] = {2, 1, 2, 3, 0};
    struct scp_stream *s = scp_stream_alloc(transport, 5, 6);
    unsigned i;
    unsigned char received[4];
    assert(s);
    s->state = SCP_ESTABLISHED; s->rcv_nxt = 0xfffffffe;
    for (i = 0; i < sizeof(order) / sizeof(order[0]); ++i) {
        struct scp_buf *packet = receive_buffer(s);
        struct scp_hdr *header = (struct scp_hdr *)packet->data;
        header->seq = htonl(0xfffffffeU + order[i]); header->len = htons(1);
        packet->data[sizeof(*header)] = (unsigned char)('A' + order[i]);
        scp_process_data(s, packet);
        check_tree_order(&s->rcv_buf_q);
        if (i == 2) assert(s->rcv_buf_q.count == 2);
        if (i == 3) assert(s->rcv_buf_q.count == 3 && scp_calc_sack(s) == 0xffffffff);
    }
    assert(s->rcv_nxt == 2 && !s->rcv_buf_q.count);
    assert(scp_recv(5, received, sizeof(received)) == sizeof(received));
    assert(!memcmp(received, "ABCD", sizeof(received)) && !s->rcv_wmem && !s->sb_cc);
    scp_stream_free(s);
    assert(!live && !scp_timer_tree.count);
    puts("PASS: out-of-order receive, duplicate suppression and stream integrity across sequence wrap");
}
static void count_timer(void *arg) { ++*(unsigned *)arg; }
static void test_timer_wrap(void)
{
    struct scp_timer early = {0}, late = {0};
    unsigned early_count = 0, late_count = 0;
    now = 0xfffffff0;
    scp_timer_create(&late, count_timer, &late_count, 32);
    scp_timer_create(&early, count_timer, &early_count, 8);
    scp_timer_process();
    assert(!early_count && !late_count);
    now = 0xfffffff8; scp_timer_process();
    assert(early_count == 1 && !late_count && !early.active && late.active);
    now = 0; scp_timer_process();
    assert(early_count == 1 && !late_count);
    now = 0x10; scp_timer_process();
    assert(early_count == 1 && late_count == 1 && !late.active);
    assert(!scp_timer_tree.count && !scp_timer_tree.first_node && !scp_timer_tree.last_node);
    now = 0;
    puts("PASS: timer expiry order across clock wraparound");
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
    test_tree_order();
    test_receive_wrap(&t);
    test_timer_wrap();
    {
        struct transfer listener;
        assert(!transfer_init(&listener, &t, 3, 4, 0, 1073741824, SCP_CC_AIMD));
        for (now = 0; now < 1000U * SCP_TICK_SCALE; ++now) {
            scp_timer_process();
            assert(!transfer_step(&listener) && !listener.failed && !listener.done);
        }
        transfer_cleanup(&listener);
        assert(live == 0);
    }
    now = 0;
    s = scp_stream_alloc(&t, 1, 2);
    assert(s && scp_is_closed(1));
    assert(!scp_stream_alloc(&t, 1, 3));
    assert(hashmap_get(&scp_stream_map, (void *)1) == s);
    assert(!scp_connect(1));
    for (now = 1; now < 13000U * SCP_TICK_SCALE; ++now) scp_timer_process();
    assert(scp_is_closed(1) && !hashmap_get(&scp_stream_map, (void *)1) && live == 0);
    s = scp_stream_alloc(&t, 1, 2); s->state = SCP_ESTABLISHED;
    assert(!scp_send(1, data, sizeof(data)));
    p = receive_buffer(s); queue_enqueue(&s->rcv_data_q, &p->list);
    p = receive_buffer(s); rb_node_init(&p->rb); p->seq = 1; rb_insert_node(&s->rcv_buf_q, &p->rb);
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
