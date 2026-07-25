#include "scp.h"
#include <stdlib.h>
#include <arpa/inet.h>
#include <string.h>
#include <stdio.h>

static void scp_send_window_probe(struct scp_stream *ss);
static void scp_retransmit(struct scp_stream *ss);
static int scp_output(struct scp_stream *ss);
static int scp_output_one(struct scp_stream *ss);
static int scp_install_cc(struct scp_stream *ss,
                          enum scp_cc_id cc_id);
void scp_output_data(struct scp_stream *ss, struct scp_buf *sb);
void scp_output_ack(struct scp_stream *ss, struct scp_hdr *data_sh);

extern uint32_t scp_now_time(void);

static struct rb_root scp_timer_tree;
static struct hashmap scp_stream_map;
static struct list_node scp_stream_queue;

#if SCP_RUN_DEBUG
    #define SCP_PRINT(...) printf(__VA_ARGS__)
#else
    #define SCP_PRINT(...) ((void)0)
#endif

static void scp_debug_hex(const char *tag, const void *buf, size_t len)
{
#if SCP_DEBUG
    const uint8_t *p = buf;

    printf("---- %s (%zu bytes) ----\n", tag, len);

    for (size_t i = 0; i < len; i++) {
        printf("%02X ", p[i]);
        if ((i + 1) % 16 == 0)
            printf("\n");
    }
    if (len % 16 != 0)
        printf("\n");

    printf("-----------------------------\n");
#endif
}

static int a = 0;
static void scp_debug_dump_tx(const char *reason,
                              const void *buf, size_t len)
{
#if SCP_DEBUG
    printf("\n[SCP TX] %s, a:%d\n", reason, a++);
    scp_debug_hex("TX Packet", buf, len);
#endif
}

static int b = 0;
static void scp_debug_dump_rx(const void *buf, size_t len)
{
#if SCP_DEBUG
    printf("\n[SCP RX] %d\n", b++);
    scp_debug_hex("RX Packet", buf, len);
#endif
}

static void scp_dump_ss_fields(struct scp_stream *ss)
{
    uint32_t flight = ss->snd_nxt - ss->snd_una;

    printf("\"snd_una\":%u,"
           "\"snd_seq_q\":%u,"
           "\"snd_nxt\":%u,"
           "\"rcv_nxt\":%u,"

           "\"snd_wnd\":%u,"
           "\"rcv_wnd\":%u,"

           "\"snd_wmem\":%u,"
           "\"rcv_wmem\":%u,"

           "\"snd_q\":%d,"
           "\"rcv_q\":%d,"

           "\"cwnd\":%u,"
           "\"flight\":%u,"
           "\"srtt\":%u,"
           "\"rto\":%u,"
           "\"dup_acks\":%u,"
           "\"sb_cc\":%u,"
           "\"packet_bytes\":%u,"
           "\"packet_count\":%u,"
           "\"cong_q\":%u,"
           "\"cong_q_ema\":%u,"
           "\"loss_cnt\":%u,"
           "\"sent_cnt\":%u,"
           "\"p\":%u,"
           "\"p_ema\":%u,"
           "\"d\":%d,"
           "\"z\":%d,"
           "\"cc_phase\":%d,"
           "\"cc_id\":%u,"
           "\"ssthresh\":%u,"
           "\"prob_mode\":%u,"
           "\"prob_gamma\":%d,"
           "\"prob_beta\":%d,"
           "\"probe_low\":%u,"
           "\"probe_prev\":%u,"
           "\"probe_noise\":%u,"
           "\"last_cycle\":%u",

           ss->snd_una,
           ss->snd_seq_q,
           ss->snd_nxt,
           ss->rcv_nxt,

           ss->snd_wnd,
           ss->rcv_wnd,

           ss->snd_wmem,
           ss->rcv_wmem,

           ss->snd_q_count,
           ss->rcv_buf_q.count,

           ss->cwnd,
           flight,

           ss->srtt,
           ss->rto,
           ss->dup_acks,
           ss->sb_cc,
           ss->packet_bytes,
           ss->packet_count,
           ss->cong_q,
           ss->cong_q_ema,
           ss->loss_cnt,
           ss->sent_cnt,
           ss->p,
           ss->p_ema,
           ss->d,
           ss->z,
           ss->cc_phase,
           (unsigned)ss->cc_id,
           ss->ssthresh,
           (unsigned)ss->prob_mode,
           ss->prob_gamma_q16,
           ss->prob_beta_q16,
           ss->prob_d_low,
           ss->prob_d_prev,
           ss->prob_noise,
           ss->prob_last_cycle
    );
}

static void scp_dump_ss_state(struct scp_stream *ss)
{
#if SCP_DUMP_SS
    printf("{\"t\":%u,\"type\":\"ss\",", scp_now_time());
    scp_dump_ss_fields(ss);
    printf("}\n");
#endif
}

static void scp_dump_hdr(struct scp_stream *ss,
                         const char *dir,
                         const struct scp_hdr *h)
{
#if SCP_DUMP_HDR
    uint32_t seq  = ntohl(h->seq);
    uint32_t ack  = ntohl(h->ack);
    uint32_t sack = ntohl(h->sack);
    uint32_t wnd  = ntohl(h->wnd);
    uint32_t len  = ntohs(h->len);

    printf("{\"t\":%u,"
           "\"type\":\"hdr\","
           "\"dir\":\"%s\","
           "\"seq\":%u,"
           "\"ack\":%u,"
           "\"sack\":%u,"
           "\"len\":%u,"
           "\"wnd\":%u,"
           "\"flags\":%u,",
           scp_now_time(),
           dir,
           seq,
           ack,
           sack,
           len,
           wnd,
           h->flags
    );

    scp_dump_ss_fields(ss);

    printf("}\n");
#endif
}

//write by yourself
void *scp_malloc(size_t want_size)
{
    return malloc(want_size);
}

void scp_free(void *ptr)
{
    free(ptr);
}

void scp_timer_init(void)
{
    rb_root_init(&scp_timer_tree);
}

void scp_timer_node_init(struct scp_timer *t)
{
    if (!t) return;

    rb_node_init(&t->node);   
    t->expire = 0;           
    t->timeout = 0;        
    t->cb = NULL;           
    t->arg = NULL;         
    t->active = 0;        
}

static inline void scp_timer_add(struct scp_timer *t)
{
    t->expire = scp_now_time() + t->timeout;
    t->node.value = t->expire;
    rb_insert_node(&scp_timer_tree, &t->node);
    t->active = 1;
}

/*
 * We must make sure the node in tree.
 * So we use t->active flags.
 */
static inline void scp_timer_remove(struct scp_timer *t)
{
    if (!t->active) return;

    rb_remove_node(&scp_timer_tree, &t->node);
    t->active = 0;
}

scp_timer_handle_t scp_timer_create(struct scp_timer *t,
                                    scp_timer_cb_t cb,
                                    void *arg,
                                    uint32_t timeout)
{
    if (!t) return NULL;

    if (t->active) scp_timer_remove(t); 

    rb_node_init(&t->node);
    t->timeout = timeout;
    t->cb = cb;
    t->arg = arg;
    t->active = 0;

    scp_timer_add(t);
    return t;
}

/*
 *malloc or free is not good,We just init it in struct stream.
*/
void scp_timer_delete(scp_timer_handle_t h)
{
    if (!h) return;
    scp_timer_remove(h);
}

/*
 * We use uint32_t, if clock update, that is bad.
 * So we use SEG_GT.
 */
void scp_timer_process(void)
{
    struct rb_node *n;

    for (;;) {
        n = scp_timer_tree.first_node;
        if (!n) break;
    
        struct scp_timer *t =
            container_of(n, struct scp_timer, node);

        if (SEQ_GT(t->expire, scp_now_time())) break;

        rb_remove_node(&scp_timer_tree, &t->node);
        t->active = 0;

        t->cb(t->arg);  
    }
}

/*
 *Update cwnd
 *If retrans too many, we close.
 *The timer is period.
*/
static void scp_timer_retrans_cb(void *arg)
{
    struct scp_stream *ss = arg;
    if (!ss || ss->state == SCP_CLOSED) return;

    if (ss->cc_on_timeout)
        ss->cc_on_timeout(ss);

    ss->dup_acks = 0;
    ss->fr_active = 0;
    ss->recover_seq = 0;

    scp_retransmit(ss);

    ss->timeout_count++;

    if (ss->timeout_count > RETRANS_COUNT_MAX) {
        SCP_PRINT("RETRANS FAIL!\r\n");

        ss->state = SCP_CLOSED;
        scp_stream_free(ss);
        return;
    }

    uint32_t next_rto = ss->rto * 2; 
    if (next_rto > SCP_RTO_MAX) 
        next_rto = SCP_RTO_MAX; 
    ss->rto = next_rto;

    scp_timer_create(&ss->t_retrans,
                scp_timer_retrans_cb,
                ss,                      
                ss->rto 
                );

}

/*
 *In zero windows, we perisit remote.
 *If don't connect in a time, it is keeplive timer.
*/
static void scp_timer_persist_cb(void *arg)
{
    struct scp_stream *ss = arg;
    if (!ss || ss->state == SCP_CLOSED)
        return;

    uint32_t now = scp_now_time();
    int need_probe = 0;

    uint32_t idle = now - ss->last_active;

    if (idle >= IDLE_TIMEOUT * (ss->idle_failures + 1)) {
        need_probe = 1;
        ss->idle_failures++;

        if (ss->idle_failures > MAX_IDLE_FAIL) {
            ss->state = SCP_CLOSED;
            SCP_PRINT("IDLE FAIL!\r\n");
            scp_stream_free(ss);
            return;
        }
    }

    if ((!list_empty(&ss->snd_q) && ss->snd_wnd <= (2*MTU)) || 
        ss->cwnd <= (2*MTU)) {
        need_probe = 1;
    }

    if (need_probe) {
        scp_send_window_probe(ss);
    }

    scp_timer_create(&ss->t_persist,
        scp_timer_persist_cb,
        ss,
        PERSIST_INTERVAL
    );
}

/*
 *For safe, iss must random.
*/
static uint32_t random32(void)
{
    uint32_t r = ((uint32_t)rand() << 16) ^ (uint32_t)rand();
    return r ? r : 1;  
}

/*
 *If our data is non orider, must rcv_buf_q first and rcv_data_q is not neibor
 *So (rcv_data_q last, rcv_buf_q first) is we need.
 */
static inline uint32_t scp_calc_sack(struct scp_stream *ss)
{
    if (ss->rcv_buf_q.rb_node == NULL)
        return ss->rcv_nxt;

    struct rb_node *n = rb_first(&ss->rcv_buf_q);
    struct scp_buf *first = container_of(n, struct scp_buf, rb);
    return first->seq;
}

/*
 * Just alloc, you can replace memory alloc.
*/
static struct scp_buf *scp_buf_alloc(uint32_t len)
{
    struct scp_buf *sb = scp_malloc(len);
    if (!sb) return NULL;

    memset(sb, 0, len);
    sb->data = (uint8_t *)sb + sizeof(struct scp_buf);

    return sb;
}

static void scp_buf_free(struct scp_stream *ss, struct scp_buf *b)
{
    uint16_t total;

    if (!b) return;
    total = sizeof(struct scp_buf) + b->len;
    if (b->dir == SCP_DIR_INPUT) {
        ss->rcv_wmem -= total;
    } else if (b->dir == SCP_DIR_SEND) {
        ss->snd_wmem -= total;
    }
    scp_free(b);
}

/*
* Init it, if have many stream, we link it.
*/
struct scp_stream *scp_stream_alloc(struct scp_transport_class *st_class, int src_fd, int dst_fd)
{
    struct scp_stream *ss = scp_malloc(sizeof(struct scp_stream));
    if (!ss) {
        return NULL;
    }

    *ss = (struct scp_stream) {
            .src_fd   = src_fd,
            .dst_fd   = dst_fd,
            .srtt = 0,
            .rttvar = 0,
            .rto = SCP_RTO_MIN,
            .rto_recovery = 0,
            .sb_hiwat = SCP_RECV_LIMIT,
            .rcv_wnd  = RECV_WIN_INIT,
            .snd_wnd  = SEND_WIN_INIT,
            .st_class = st_class,
            .state = SCP_CLOSED,
            .iss = 0,
            .rcv_nxt = 0,
            .snd_seq_q = 0,
            .snd_nxt = 0,
            .snd_una = 0,
            .cwnd = MTU, 
            .dup_acks = 0,
            .last_gap_rexmit_ack = 0,
            .fr_active = 0,
            .packet_bytes = 0,
            .packet_count = 0,
            .sent_cnt = 0,
            .loss_cnt = 0,
            .p_ema = 0,
            .cong_q = 0,
            .cc_phase = SCP_CC_PHASE_INC
    };

    scp_timer_node_init(&ss->t_retrans);
    scp_timer_node_init(&ss->t_persist);
    scp_timer_node_init(&ss->t_hs);
    scp_timer_node_init(&ss->t_fin);

    list_node_init(&ss->node);
    list_node_init(&ss->snd_q);
    rb_root_init(&ss->rcv_buf_q);
    list_node_init(&ss->rcv_data_q);

    if (scp_install_cc(ss, SCP_CC_PROB) != 0) {
        scp_free(ss);
        return NULL;
    }

    queue_enqueue(&scp_stream_queue, &ss->node);
    hashmap_put(&scp_stream_map, (void *)(uintptr_t)(ss->src_fd), ss);

    return ss;
}

int scp_stream_free(struct scp_stream *ss)
{
    if (!ss) return -1;

    scp_timer_remove(&ss->t_retrans);
    scp_timer_remove(&ss->t_persist);
    scp_timer_remove(&ss->t_hs);
    scp_timer_remove(&ss->t_fin);

    list_remove(&ss->node);
    hashmap_remove(&scp_stream_map, (void *)(uintptr_t)(ss->src_fd));

    scp_free(ss);
    return 0;
}

/*
 * A fd is a stream, we need to record them.
 * list init and hashmap, list link stream.
 * hashmap put fd.
*/
int scp_init(size_t max_streams)
{
    list_node_init(&scp_stream_queue);
    hashmap_init(&scp_stream_map, max_streams, HASHMAP_KEY_INT);
    return 0;
}

/*
* sb_hiwat is limit, and can't overout.
*/
static inline void scp_update_rcv_wnd(struct scp_stream *s)
{
    if (s->sb_cc >= s->sb_hiwat)
        s->rcv_wnd = 0;
    else
        s->rcv_wnd = s->sb_hiwat - s->sb_cc;
}

/*
 * When no respond, we need to get information from remote.
 * So ping it, get wnd or other.
 */
static void scp_send_window_probe(struct scp_stream *ss)
{
    struct scp_hdr hdr = {
            .seq   = htonl(ss->snd_nxt),
            .ack   = htonl(ss->rcv_nxt),
            .sack  = htonl(scp_calc_sack(ss)),
            .wnd   = htonl(ss->rcv_wnd),
            .len   = 0,
            .cksum = 0,
            .flags = SCP_FLAG_PING,
            .fd    = ss->dst_fd,
    };

    hdr.cksum = in_checksum(&hdr, sizeof(hdr));
    scp_dump_hdr(ss, "WINDOWS_PING", &hdr);
    ss->st_class->send(ss->st_class->user, &hdr, sizeof(hdr));
}

/*
 * SACK-based selective retransmit.
 * Resend all bytes in [gap_start, gap_end) from snd_q.
 * rely on cwnd path for congestion control.
 */
void scp_retransmit_gap(struct scp_stream *ss,
                        uint32_t gap_start,
                        uint32_t gap_end)
{
    struct list_node *n;
    int sent_frags = 0;

    for (n = ss->snd_q.next; n != &ss->snd_q; n = n->next) {
        struct scp_buf *sb = container_of(n, struct scp_buf, list);
        uint32_t total = sb->len - sizeof(struct scp_hdr);

        uint32_t seg_start = sb->seq;
        uint32_t seg_end   = sb->seq + total;

        // skip segments fully before gap
        if (SEQ_LEQ(seg_end, gap_start))
            continue;

        // stop when segment starts after gap
        if (SEQ_GEQ(seg_start, gap_end))
            return;

        ss->loss_cnt++;
        scp_output_data(ss, sb); 
        sent_frags++;

        if (sent_frags >= RETRANS_GAP_MAX)
            return;

    }
}

/*
 * RTO-based retransmit (safe version).
 * Only retransmit the first unacked segment (snd_una).
 * This avoids queue explosion and RTO storms.
 */
static void scp_retransmit(struct scp_stream *ss)
{
    uint32_t max_frags = 1 << ss->rto_recovery;

    if (max_frags > RETRANS_RECO_MAX)
        max_frags = RETRANS_RECO_MAX;

    uint32_t sent = 0;

    struct list_node *n;
    for (n = ss->snd_q.next; n != &ss->snd_q; n = n->next) {
        struct scp_buf *sb = container_of(n, struct scp_buf, list);
        uint32_t total = sb->len - sizeof(struct scp_hdr);

        uint32_t seg_start = sb->seq;
        uint32_t seg_end   = sb->seq + total;

        if (SEQ_LEQ(seg_end, ss->snd_una))
            continue;

        ss->loss_cnt++;
        scp_output_data(ss, sb);
        sent++;

        if (sent >= max_frags)
            return;
    }
}

/*
* Just for swap iss.
*/
static void scp_output_connect(struct scp_stream *ss)
{
    struct scp_hdr hdr = {
        .seq = htonl(ss->iss),
        .ack = 0,
        .sack = htonl(scp_calc_sack(ss)),
        .wnd = htonl(ss->rcv_wnd),
        .len = 0,
        .flags = SCP_FLAG_CONNECT,
        .fd = ss->dst_fd,
    };
    hdr.cksum = in_checksum(&hdr, sizeof(hdr));
    scp_debug_dump_tx("CONNECT", &hdr, sizeof(hdr));
    scp_dump_hdr(ss, "CONNECT", &hdr);
    ss->st_class->send(ss->st_class->user, &hdr, sizeof(hdr));
}

/*
* Swap iss.
*/
static void scp_output_connect_ack(struct scp_stream *ss)
{
    struct scp_hdr hdr = {
        .seq = htonl(ss->snd_nxt),
        .ack = htonl(ss->rcv_nxt),
        .sack = htonl(scp_calc_sack(ss)),
        .wnd = htonl(ss->rcv_wnd),
        .len = 0,
        .flags = SCP_FLAG_CONNECT_ACK,
        .fd = ss->dst_fd,
    };
    hdr.cksum = 0;
    hdr.cksum = in_checksum(&hdr, sizeof(hdr));
    scp_debug_dump_tx("CONNECT_ACK", &hdr, sizeof(hdr));
    scp_dump_hdr(ss, "CONNECT_ACK", &hdr);
    ss->st_class->send(ss->st_class->user, &hdr, sizeof(hdr));
}

static void scp_output_fin_first(struct scp_stream *ss)
{
    struct scp_hdr hdr = {0};

    hdr.seq  = htonl(ss->snd_nxt);
    hdr.ack  = htonl(ss->rcv_nxt);
    hdr.sack = htonl(scp_calc_sack(ss));
    hdr.wnd  = htonl(ss->rcv_wnd);
    hdr.len  = 0;
    hdr.flags = SCP_FLAG_FIN | SCP_FLAG_ACK;
    hdr.fd   = ss->dst_fd;
    hdr.cksum = 0;
    hdr.cksum = in_checksum(&hdr, sizeof(hdr));

    scp_debug_dump_tx("FIN_ACK", &hdr, sizeof(hdr));
    scp_dump_hdr(ss, "FIN_ACK", &hdr);
    ss->st_class->send(ss->st_class->user, &hdr, sizeof(hdr));

    ss->snd_nxt++;   
}

static void scp_output_fin_retrans(struct scp_stream *ss)
{
    struct scp_hdr hdr = {0};

    hdr.seq  = htonl(ss->snd_nxt - 1);  
    hdr.ack  = htonl(ss->rcv_nxt);
    hdr.sack = htonl(scp_calc_sack(ss));
    hdr.wnd  = htonl(ss->rcv_wnd);
    hdr.len  = 0;
    hdr.flags = SCP_FLAG_FIN | SCP_FLAG_ACK;
    hdr.fd   = ss->dst_fd;
    hdr.cksum = 0;
    hdr.cksum = in_checksum(&hdr, sizeof(hdr));

    scp_debug_dump_tx("FIN_RETRANS", &hdr, sizeof(hdr));
    scp_dump_hdr(ss, "FIN_RETRANS", &hdr);
    ss->st_class->send(ss->st_class->user, &hdr, sizeof(hdr));
}


/*
*For handshake timeout, we need to retrans it.
*/
static void scp_handle_handshake_timeout(void *arg)
{
    struct scp_stream *ss = arg;
    if (!ss) return;

    if (ss->state != SCP_SYN_SENT && ss->state != SCP_SYN_RECV) {
        scp_timer_delete(&ss->t_hs);  
        return;
    }

    if (ss->retry++ < 5) {

        if (ss->state == SCP_SYN_SENT)
            scp_output_connect(ss);
        else
            scp_output_connect_ack(ss);

        scp_timer_delete(&ss->t_hs);
        scp_timer_create(&ss->t_hs,
            scp_handle_handshake_timeout,
            ss,
            ss->rto << ss->retry
        );

        return;
    }

    ss->state = SCP_CLOSED;

    scp_timer_delete(&ss->t_hs);
}

static void scp_handle_fin_timeout(void *arg)
{
    struct scp_stream *ss = arg;
    if (!ss) return;

    if (ss->state != SCP_FIN_WAIT && ss->state != SCP_LAST_ACK) {
        scp_timer_delete(&ss->t_fin);
        return;
    }

    if (ss->retry++ < 5) {
        scp_output_fin_retrans(ss);

        scp_timer_delete(&ss->t_fin);
        scp_timer_create(&ss->t_fin,
            scp_handle_fin_timeout,
            ss,
            ss->rto << ss->retry
        );

        return;
    }

    ss->state = SCP_CLOSED;

    scp_timer_delete(&ss->t_fin);
    scp_stream_free(ss);
}

/*
* Now we are segment, don't need to trim or others.
*/
static void scp_process_data(struct scp_stream *s, struct scp_buf *sb)
{
    struct scp_hdr *sh = (struct scp_hdr *)sb->data;
    uint32_t seq         = ntohl(sh->seq);
    uint32_t payload_len = ntohs(sh->len);

    // zero-length segment, nothing to deliver
    if (payload_len == 0) {
        scp_buf_free(s, sb);
        return;
    }

    sb->seq = seq;
    uint32_t end = seq + payload_len;

    // remove obsolete out-of-order segments already covered by rcv_nxt
    {
        struct rb_node *n = rb_first(&s->rcv_buf_q);
        while (n) {
            struct scp_buf *b = container_of(n, struct scp_buf, rb);
            struct rb_node *next = rb_next(n);

            uint32_t b_end = b->seq + (b->len - sizeof(struct scp_hdr));
            if (SEQ_LEQ(b_end, s->rcv_nxt)) {
                rb_remove_node(&s->rcv_buf_q, &b->rb);
                scp_buf_free(s, b);
                n = next;
                continue;
            }
            break;
        }
    }

    // fully duplicate segment (already consumed)
    if (SEQ_LEQ(end, s->rcv_nxt)) {
        scp_output_ack(s, sh);
        scp_buf_free(s, sb);
        return;
    }

    // in-order segment
    if (SEQ_EQ(seq, s->rcv_nxt)) {
        s->rcv_nxt += payload_len;
        queue_enqueue(&s->rcv_data_q, &sb->list);
        s->sb_cc += payload_len;
        scp_update_rcv_wnd(s);

        // consume any buffered contiguous out-of-order segments
        for (;;) {
            struct rb_node *n = rb_first(&s->rcv_buf_q);
            if (!n)
                break;

            struct scp_buf *b = container_of(n, struct scp_buf, rb);
            uint32_t b_seq  = b->seq;
            uint32_t b_plen = b->len - sizeof(struct scp_hdr);

            if (!SEQ_EQ(b_seq, s->rcv_nxt))
                break;

            rb_remove_node(&s->rcv_buf_q, &b->rb);
            queue_enqueue(&s->rcv_data_q, &b->list);
            s->rcv_nxt += b_plen;
            s->sb_cc += b_plen;
            scp_update_rcv_wnd(s);
        }

        scp_output_ack(s, sh);
        return;
    }

    // out-of-order segment: check duplicate before inserting
    {
        struct rb_node *pos = rb_first_greater(&s->rcv_buf_q, seq);
        struct rb_node *prev = NULL;

        if (pos)
            prev = rb_prev(pos);
        else
            prev = rb_last(&s->rcv_buf_q);

        // duplicate out-of-order segment
        if (prev) {
            struct scp_buf *pb = container_of(prev, struct scp_buf, rb);
            uint32_t p_seq = pb->seq;
            uint32_t p_end = p_seq + (pb->len - sizeof(struct scp_hdr));

            // full coverage: drop
            if (SEQ_GT(p_end, seq) && SEQ_GEQ(p_end, end)) {
                scp_output_ack(s, sh);
                scp_buf_free(s, sb);
                return;
            }
        }
    }

    // new out-of-order segment
    rb_node_init(&sb->rb);
    sb->rb.value = sb->seq;
    rb_insert_node(&s->rcv_buf_q, &sb->rb);
    scp_output_ack(s, sh);
}

/*
*When get ack, remote has acked, must free buf.
*/
void scp_snd_buf_free(struct scp_stream *ss, uint32_t ack)
{
    struct list_node *cur = ss->snd_q.next;

    while (cur != &ss->snd_q) {
        struct scp_buf *sb = container_of(cur, struct scp_buf, list);
        struct list_node *next = cur->next;

        uint32_t payload_len = sb->len - sizeof(struct scp_hdr);
        uint32_t end_seq     = sb->seq + payload_len;

        if (SEQ_LEQ(end_seq, ack)) {
            list_remove(cur);
            scp_buf_free(ss, sb);
            ss->snd_q_count--;
            cur = next;
            continue;
        }

        break;
    }
}

static inline void scp_update_loss(struct scp_stream *ss)
{
    if (ss->sent_cnt < P_WND)
        return;

    uint32_t p_sample =
        (uint32_t)(((uint64_t)ss->loss_cnt << 16) / ss->sent_cnt);

    if (ss->p == 0)
        ss->p = p_sample;
    else
        ss->p = (ss->p * 7 + p_sample) >> 3;

    if (ss->p_ema == 0)
        ss->p_ema = p_sample;
    else
        ss->p_ema = (ss->p_ema * 31 + p_sample) >> 5;

    ss->sent_cnt = 0;
    ss->loss_cnt = 0;
}


/*
 * RTT Estimation (RFC 6298 style, Karn’s Algorithm)
 *
 * We need to konw the time from send to acked,
 * and estimate average time and tolrent timeout.
 *
 * All intermediate calculations use signed 32‑bit integers to avoid
 * wrap‑around when (abs_delta - rttvar) becomes negative.
 */
static void scp_update_rtt(struct scp_stream *s)
{
    uint32_t rtt_sample = s->cc_on_rtt(s); 
    if (s->srtt == 0) {
        s->srtt   = rtt_sample;
        s->rttvar = rtt_sample >> 1;
        s->rto    = s->srtt + (s->rttvar << 2);
        if (s->rto < SCP_RTO_MIN) s->rto = SCP_RTO_MIN;
        if (s->rto > SCP_RTO_MAX) s->rto = SCP_RTO_MAX;
        return;
    }

    int32_t delta = (int32_t)rtt_sample - (int32_t)s->srtt;

    s->srtt = s->srtt + (delta >> 3);  

    int32_t abs_delta = delta >= 0 ? delta : -delta;
    int32_t rttvar_i  = (int32_t)s->rttvar;
    int32_t diff      = abs_delta - rttvar_i;

    rttvar_i = rttvar_i + (diff >> 2); 
    if (rttvar_i < 1) rttvar_i = 1;   

    s->rttvar = (uint32_t)rttvar_i;

    uint32_t rto = s->srtt + (s->rttvar << 2);
    if (rto < SCP_RTO_MIN) rto = SCP_RTO_MIN;
    if (rto > SCP_RTO_MAX) rto = SCP_RTO_MAX;
    s->rto = rto;
}

static inline void scp_update_rtt_base(struct scp_stream *ss)
{
    uint32_t rtt = ss->rtt_sample;
    if (rtt == 0)
        return;

    if (ss->rtt_base == 0) {
        ss->rtt_base = rtt;
        return;
    }

    if (rtt < ss->rtt_base) {
        ss->rtt_base = rtt;
    }
}

/* 
 * An ACK tells us the peer has received data.
 * We update RTT from this sample and free sent buffers.
 * The ACK also reflects the peer’s congestion state.
 * Handle ACK: update RTT, free bufferr, run SB.
 */
static void scp_process_ack(struct scp_stream *ss,
                            uint32_t ack,
                            uint32_t wnd,
                            uint32_t sack)
{
    uint32_t old_una;
    if (SEQ_LT(ack, ss->snd_una) || SEQ_GT(ack, ss->snd_nxt))
        goto out;

    old_una = ss->snd_una;

    if (SEQ_GT(ack, old_una)) {
        uint32_t acked;
        acked = ack - old_una;

        scp_update_loss(ss);    

        if (ss->fr_active &&
            SEQ_GEQ(ack, ss->recover_seq)) {
            ss->fr_active = 0;
            ss->recover_seq = 0;
        }

        if (ss->cc_on_ack)
            ss->cc_on_ack(ss, acked);

        ss->rto_recovery = 0;
    }

    ss->snd_una = ack;
    ss->snd_wnd = wnd;

    if (ss->rto == SCP_RTO_MAX) {
        ss->rto_recovery++;
    }

    scp_snd_buf_free(ss, ack);

    if (SEQ_GT(sack, ack)) {
        if (SEQ_GT(ack, ss->last_gap_rexmit_ack)) {
            scp_retransmit_gap(ss, ack, sack);
            ss->last_gap_rexmit_ack = ack;
        }
    }

    // New data ACKed
    if (SEQ_GT(ss->snd_una, old_una)) {
        ss->timeout_count = 0;
        ss->dup_acks = 0;

        scp_timer_delete(&ss->t_retrans);
        if (ss->snd_una != ss->snd_nxt) {
            scp_timer_create(&ss->t_retrans,
                             scp_timer_retrans_cb,
                             ss,
                             ss->rto);
        }

        goto out;
    }

    // No new ACK: dupACK path 
    if (SEQ_LT(ss->snd_una, ss->snd_nxt)) {
        ss->dup_acks++;

        // Enter Fast Recovery 
        if (ss->dup_acks >= 3 && !ss->fr_active) {
            ss->fr_active = 1;
            ss->recover_seq = ss->snd_nxt;

            if (ss->cc_on_loss)
                ss->cc_on_loss(ss);

            scp_retransmit_gap(ss, ack, sack); 
            ss->last_gap_rexmit_ack = ack; 

            scp_timer_delete(&ss->t_retrans);
            scp_timer_create(&ss->t_retrans,
                             scp_timer_retrans_cb,
                             ss,
                             ss->rto);
        }
    }

out:
    if (!list_empty(&ss->snd_q)) {
        scp_output(ss);
    }
}

/*
 *If we send data or ping, return ack.
 */
void scp_output_ack(struct scp_stream *ss, struct scp_hdr *data_sh)
{
    struct scp_hdr sh;
    memset(&sh, 0, sizeof(sh));

    sh.seq = htonl(ss->snd_nxt);
    sh.ack = htonl(ss->rcv_nxt);
    sh.sack = htonl(scp_calc_sack(ss));
    sh.wnd   = htonl(ss->rcv_wnd);
    sh.len   = 0;
    sh.flags = SCP_FLAG_ACK;
    sh.fd    = ss->dst_fd;
    if (data_sh && data_sh->time_val) {
        sh.time_val = data_sh->time_val;
    }

    sh.cksum = 0;
    sh.cksum = in_checksum(&sh, sizeof(struct scp_hdr));

    scp_debug_dump_tx("ACK", &sh, sizeof(struct scp_hdr));
    scp_dump_hdr(ss, "ACK", &sh);
    ss->st_class->send(ss->st_class->user, &sh, sizeof(struct scp_hdr));
}

/* Build a transmit-ready packet for this fragment.
 * sb->data holds only payload; every send needs a fresh header.
 * Fragments may start at any offset, so we must assemble [hdr][payload].
 * sb->data is persistent state and must not be modified in-place.
 */
void scp_output_data(struct scp_stream *ss, struct scp_buf *sb)
{
    struct scp_hdr *hdr = (struct scp_hdr *)sb->data;

    hdr->seq   = htonl(sb->seq);
    hdr->ack   = htonl(ss->rcv_nxt);
    hdr->sack  = htonl(scp_calc_sack(ss));
    hdr->wnd   = htonl(ss->rcv_wnd);
    hdr->len   = htons((uint16_t)sb->len - sizeof(struct scp_hdr));
    hdr->flags = SCP_FLAG_DATA;
    hdr->fd    = ss->dst_fd;
    hdr->time_val  = htonl(scp_now_time());

    hdr->cksum = 0;
    hdr->cksum = in_checksum(sb->data, sb->len);

    scp_debug_dump_tx("DATA", sb->data, sb->len);
    scp_dump_hdr(ss, "DATA", hdr);
    ss->st_class->send(ss->st_class->user, sb->data, sb->len);

    ss->packet_count++;
    ss->packet_bytes += sb->len - sizeof(struct scp_hdr);
    ss->sent_cnt++;
}

static int scp_output_one(struct scp_stream *ss)
{
    struct list_node *n = NULL;

    for (n = ss->snd_q.next; n != &ss->snd_q; n = n->next) {
        struct scp_buf *sb = container_of(n, struct scp_buf, list);

        if (sb->sent)
            continue;

        uint32_t frag_len = sb->len - sizeof(struct scp_hdr);
        uint32_t end_seq = sb->seq + frag_len;

        scp_output_data(ss, sb);

        sb->sent = 1;

        if (SEQ_GT(end_seq, ss->snd_nxt))
            ss->snd_nxt = end_seq;

        return (int)frag_len;
    }

    return 0;
}

static int scp_output(struct scp_stream *ss)
{
    uint32_t flight = ss->snd_nxt - ss->snd_una;

    uint32_t effective_win = min(ss->snd_wnd, ss->cwnd);
    if (effective_win == 0 || flight >= effective_win)
        return 0;

    for (;;) {
        uint32_t win = min(ss->snd_wnd, ss->cwnd);

        int32_t swnd = (int32_t)win - (int32_t)flight;
        if (swnd <= 0)
            break;

        int frag = scp_output_one(ss);
        if (frag <= 0)
            break;

        flight += (uint32_t)frag;
    }

    scp_timer_create(&ss->t_retrans,
                     scp_timer_retrans_cb,
                     ss,
                     ss->rto);
    ss->timeout_count = 0;

    return 0;
}

/*
* swap iss. start timer.
*/
int scp_connect(int fd)
{
    struct scp_stream *ss = hashmap_get(&scp_stream_map, (void *)(uintptr_t)fd);
    if (!ss || ss->state != SCP_CLOSED)
        return -1;

    ss->iss      = random32();
    ss->snd_seq_q  = ss->iss;
    ss->snd_una  = ss->iss;
    ss->snd_nxt = ss->iss;

    ss->state = SCP_SYN_SENT;
    ss->retry = 0;

    scp_output_connect(ss);

    scp_timer_delete(&ss->t_hs);
    scp_timer_create(&ss->t_hs,
        scp_handle_handshake_timeout, 
        ss,                         
        ss->rto                      
    );

    return 0;
}

/*Copy or not copy, this is a question.
* Just enqueue.
*/
int scp_send(int fd, void *buf, size_t len)
{
    struct scp_stream *ss = hashmap_get(&scp_stream_map, (void *)(uintptr_t)fd);
    if (!ss) return -1;
    if (ss->state != SCP_ESTABLISHED) return -1;

    uint32_t flight = ss->snd_nxt - ss->snd_una;
    uint32_t win = ss->snd_wnd;
    if (flight >= win) return SCP_ERR_NOBUF;

    uint32_t off = 0;
    while (off < len) {
        uint32_t frag = (len - off > MSS) ? MSS : (uint32_t)(len - off);
        uint32_t total = sizeof(struct scp_buf) + sizeof(struct scp_hdr) + frag;

        if (ss->snd_wmem + total > SEND_MEM_MAX) return SCP_ERR_NOBUF;
        ss->snd_wmem += total;

        struct scp_buf *sb = scp_buf_alloc(total);
        if (!sb) return SCP_ERR_NOMEM;

        sb->data = (uint8_t *)sb + sizeof(struct scp_buf);
        sb->len  = sizeof(struct scp_hdr) + frag;
        sb->seq  = ss->snd_seq_q;
        sb->dir = SCP_DIR_SEND;

        ss->snd_seq_q += frag;

        uint8_t *pure = sb->data + sizeof(struct scp_hdr);
        memcpy(pure, (uint8_t *)buf + off, frag);
        queue_enqueue(&ss->snd_q, &sb->list);
        ss->snd_q_count++;

        off      += frag;
    }

    scp_output(ss);
    return 0;
}

/*
*To swap iss, respond syn+ack.
*/
static void scp_listen_like_process(struct scp_stream *ss,
                              struct scp_buf *sb,
                              uint32_t ack,
                              uint32_t wnd)
{
    struct scp_hdr *sh = (struct scp_hdr *)sb->data;
    if (sh->flags & SCP_FLAG_CONNECT) {

        ss->state = SCP_SYN_RECV;

        ss->irs     = ntohl(sh->seq);
        ss->rcv_nxt = ss->irs;

        ss->iss      = random32();
        ss->snd_seq_q  = ss->iss;
        ss->snd_una  = ss->iss;
        ss->snd_nxt = ss->iss;

        scp_output_connect_ack(ss);

        ss->retry = 0;

        scp_timer_delete(&ss->t_hs);
        scp_timer_create(&ss->t_hs,
            scp_handle_handshake_timeout,  
            ss,                           
            ss->rto                        
        );
    }

    scp_buf_free(ss, sb);
}

/*
* handshake has done, delete t_hs.  
* start persist for zeor wnd and keeplive.
*/
static void scp_syn_sent_process(struct scp_stream *ss,
                                 struct scp_buf *sb,
                                 uint32_t ack,
                                 uint32_t wnd)
{
    struct scp_hdr *sh = (struct scp_hdr *)sb->data;
    if (sh->flags & SCP_FLAG_CONNECT_ACK) {

        ss->irs = ntohl(sh->seq);
        ss->rcv_nxt = ss->irs;

        scp_output_ack(ss, NULL);

        ss->state = SCP_ESTABLISHED;

        scp_timer_delete(&ss->t_hs);

        scp_timer_create(&ss->t_persist,
                scp_timer_persist_cb,
                ss,
                PERSIST_INTERVAL
            );
    }

    scp_buf_free(ss, sb);
}

static void scp_syn_recv_process(struct scp_stream *ss,
                                 struct scp_buf *sb,
                                 uint32_t ack,
                                 uint32_t wnd)
{
    struct scp_hdr *sh = (struct scp_hdr *)sb->data;
    if (sh->flags & SCP_FLAG_ACK) {
        ss->state = SCP_ESTABLISHED;

        scp_timer_delete(&ss->t_hs);

        scp_timer_create(&ss->t_persist,
                scp_timer_persist_cb,
                ss,
                PERSIST_INTERVAL
            );

        if (sh->flags & SCP_FLAG_DATA) {
            scp_process_data(ss, sb);
            return;
        }

        scp_buf_free(ss, sb);
        return;
    }

    scp_buf_free(ss, sb);
}

/* Process ACK/PING first to update send state immediately.
 * FIN consumes one sequence number and must be acknowledged reliably.
 * DATA is delivered or buffered according to rcv_nxt.
 * Any unhandled packet is freed after state‑specific processing.
 */
static void scp_est_process(struct scp_stream *ss,
                            struct scp_buf *sb,
                            uint32_t ack,
                            uint32_t wnd,
                            uint32_t sack)
{
    struct scp_hdr *sh = (struct scp_hdr *)sb->data;
    if (sh->flags & SCP_FLAG_ACK) {
        /*
         * Only a pure ACK carries an echoed DATA timestamp.
         *
         * FIN|ACK and other control packets do not carry an RTT echo.
         * Reading their time_val would interpret an unrelated or
         * uninitialized value as a send timestamp and could make the
         * unsigned subtraction wrap to a value near UINT32_MAX.
         */
        if (sh->flags == SCP_FLAG_ACK) {
            uint32_t time_val = ntohl(sh->time_val);

            if (time_val) {
                uint32_t sample = scp_now_time() - time_val;

                if (!sample)
                    sample = 1;

                ss->rtt_sample = sample;
                scp_update_rtt_base(ss);
                scp_update_rtt(ss);
                scp_dump_ss_state(ss);
            }
        }

        scp_process_ack(ss, ack, wnd, sack);
    }

    if (sh->flags & SCP_FLAG_DATA) {
        scp_process_data(ss, sb);
        return;
    }

    if (sh->flags & SCP_FLAG_PING) {
        scp_output_ack(ss, NULL);
    }

    if (sh->flags & SCP_FLAG_FIN) {
        uint32_t seq = ntohl(sh->seq);

        if (SEQ_LT(seq, ss->rcv_nxt)) {
            scp_output_ack(ss, NULL);
            scp_buf_free(ss, sb);
            return;
        }

        ss->rcv_nxt = seq + 1;
        scp_output_ack(ss, NULL);

        ss->state = SCP_LAST_ACK;
        ss->retry = 0;

        scp_timer_create(&ss->t_fin,
            scp_handle_fin_timeout,
            ss,
            ss->rto
        );

        scp_output_fin_first(ss);
        scp_buf_free(ss, sb);
        return;
    }

    scp_buf_free(ss, sb);
}

static void scp_fin_wait_process(struct scp_stream *ss,
                                 struct scp_buf *sb,
                                 uint32_t ack,
                                 uint32_t wnd,
                                 uint32_t sack)
{
    struct scp_hdr *sh = (struct scp_hdr *)sb->data;
    if (sh->flags & SCP_FLAG_ACK) {
        scp_process_ack(ss, ack, wnd, sack);
    }

    if (sh->flags & SCP_FLAG_PING) {
        scp_output_ack(ss, NULL);
    }

    if (sh->flags & SCP_FLAG_DATA) {
        scp_output_ack(ss, sh);
        scp_buf_free(ss, sb);
        return;
    }

    if (sh->flags & SCP_FLAG_FIN) {
        uint32_t seq = ntohl(sh->seq);
        if (SEQ_GEQ(seq, ss->rcv_nxt)) {
            ss->rcv_nxt = seq + 1;
        }

        scp_output_ack(ss, NULL);

        scp_timer_delete(&ss->t_fin);

        ss->state = SCP_CLOSED;
        scp_buf_free(ss, sb);
        scp_stream_free(ss);
        return;
    }

    scp_buf_free(ss, sb);
}

static void scp_last_ack_process(struct scp_stream *ss,
                                 struct scp_buf *sb,
                                 uint32_t ack,
                                 uint32_t wnd,
                                 uint32_t sack)
{
    struct scp_hdr *sh = (struct scp_hdr *)sb->data;
    if (sh->flags & SCP_FLAG_PING) {
        scp_output_ack(ss, NULL);
    }

    if (sh->flags & SCP_FLAG_FIN) {
        scp_output_ack(ss, NULL);
        scp_buf_free(ss, sb);
        return;
    }

    if (sh->flags & SCP_FLAG_ACK) {
        scp_timer_delete(&ss->t_fin);

        ss->state = SCP_CLOSED;
        scp_buf_free(ss, sb);
        scp_stream_free(ss);
        return;
    }

    scp_buf_free(ss, sb);
}

/*
*Copy or not copy, this is a question.
*We need record all data, then update keeplive
*/
int scp_input(void *ctx, void *buf, size_t len)
{
    struct scp_buf *sb;
    struct scp_hdr *sh;
    struct scp_stream *ss;
    uint32_t total;

    scp_debug_dump_rx(buf, len);

    sh = (struct scp_hdr *)buf;

    uint16_t calc = in_checksum(buf, len);
    if (calc != 0) return -1;

    uint32_t ack = ntohl(sh->ack);
    uint32_t wnd = ntohl(sh->wnd);
    uint32_t sack = ntohl(sh->sack);

    ss = hashmap_get(&scp_stream_map, (void *)(uintptr_t)sh->fd);
    if (!ss) return -1;

    total = sizeof(struct scp_buf) + len;
    if (ss->rcv_wmem + total > RECV_MEM_MAX)
        return SCP_ERR_NOBUF;

    ss->rcv_wmem += total;
    ss->last_active = scp_now_time(); 
    ss->idle_failures = 0;

    sb = scp_buf_alloc(total);
    if (!sb) return SCP_ERR_NOMEM;

    memcpy(sb->data, buf, len);
    sb->len = len;
    sb->dir = SCP_DIR_INPUT;

    switch (ss->state) {
    case SCP_CLOSED:
        scp_listen_like_process(ss, sb, ack, wnd);
        break;
    case SCP_SYN_SENT:
        scp_syn_sent_process(ss, sb, ack, wnd);
        break;
    case SCP_SYN_RECV:
        scp_syn_recv_process(ss, sb, ack, wnd);
        break;
    case SCP_ESTABLISHED:
        scp_est_process(ss, sb, ack, wnd, sack);
        break;
    case SCP_FIN_WAIT:
        scp_fin_wait_process(ss, sb, ack, wnd, sack);
        break;
    case SCP_LAST_ACK:
        scp_last_ack_process(ss, sb, ack, wnd, sack);
        break;
    default:
        scp_buf_free(ss, sb);
        break;
    }

    return 0;
}

/*
* We need to close all in all state.
*/
void scp_close(int fd)
{
    struct scp_stream *ss = hashmap_get(&scp_stream_map, (void *)(uintptr_t)fd);
    if (!ss) return;

    switch (ss->state) {

    case SCP_ESTABLISHED:
        ss->state = SCP_FIN_WAIT;
        ss->retry = 0;

        scp_timer_delete(&ss->t_fin);
        scp_timer_create(&ss->t_fin,
            scp_handle_fin_timeout,
            ss,
            ss->rto
        );

        scp_output_fin_first(ss);
        break;

    case SCP_SYN_SENT:
    case SCP_SYN_RECV:
        ss->state = SCP_CLOSED;
        scp_stream_free(ss);
        break;

    case SCP_FIN_WAIT:
    case SCP_LAST_ACK:
        break;

    case SCP_CLOSED:
    default:
        scp_stream_free(ss);
        break;
    }
}

/*
*Get data from rcv_data_q, update sb_cc and wnd.
*If we are zeor wnd, must update.
*/
int scp_recv(int fd, void *buf, size_t len)
{
    struct scp_stream *s = hashmap_get(&scp_stream_map, (void *)(uintptr_t)fd);
    if (!s) return -1;

    uint8_t *dst = buf;
    size_t copied = 0;

    while (copied < len && !list_empty(&s->rcv_data_q)) {

        struct list_node *n = s->rcv_data_q.next;
        struct scp_buf *sb = container_of(n, struct scp_buf, list);

        uint32_t total_payload = sb->len - sizeof(struct scp_hdr);
        uint32_t payload_len   = total_payload - sb->payload_off;

        uint32_t take = (uint32_t)min(len - copied, payload_len);
        uint8_t *payload = sb->data + sizeof(struct scp_hdr) + sb->payload_off;

        memcpy(dst + copied, payload, take);
        copied += take;

        if (take == payload_len) {
            list_remove(n);
            scp_buf_free(s, sb);
        } else {
            sb->payload_off += take;
        }
    }

    if (copied > s->sb_cc) {
        s->sb_cc = 0;
    } else {
        s->sb_cc -= copied;
    }

    uint32_t old_wnd = s->rcv_wnd;
    scp_update_rcv_wnd(s);

    if (old_wnd == 0 && s->rcv_wnd > 0) {
        scp_output_ack(s, NULL);
    }

    return copied;
}

int scp_is_closed(int fd)
{
    struct scp_stream *ss =
        hashmap_get(&scp_stream_map,
                    (void *)(uintptr_t)fd);

    return ss == NULL;
}


#include <math.h>
//if you want to know why, please see docs.

/*
 * Hysteresis boundaries:
 *
 * q_low  = 2/5 = 0.4
 * q_high = 3/5 = 0.6
 *
 * 65535 is exactly divisible by 5.
 */
#define SCP_Q_LOW_U16   ((uint16_t)26214U)
#define SCP_Q_HIGH_U16  ((uint16_t)39321U)

/*
 * Lambda = 2 expansion in logit space:
 *
 * q_dec_star = 4/13 ≈ 0.307692
 * q_inc_star = 9/13 ≈ 0.692308
 */
#define SCP_Q_DEC_STAR  (4.0f / 13.0f)
#define SCP_Q_INC_STAR  (9.0f / 13.0f)

#define Q16_ONE   (1 << 16)
#define Q16_INT(x) ((int32_t)((x) << 16))
#define Q16_FLOAT(x) ((int32_t)((x) * 65536.0f))
#define Q16(x) ((int32_t)((x) << 16))

#define NEG20       Q16(-20)
#define POS20       Q16(20)

/*
 * Probe policy. These are controller time-scale constants, not
 * path-specific RTT or bandwidth parameters.
 */
#define SCP_PROB_REPROBE_MS      10000U
#define SCP_PROB_STABLE_EPOCHS   3U
#define SCP_PROB_RISE_EPOCHS     2U

static inline uint16_t logistic(int32_t z)
{
    float zf = (float)z / 65536.0f;

    if (zf > 20.0f) zf = 20.0f;
    if (zf < -20.0f) zf = -20.0f;

    float qf = 1.0f / (1.0f + expf(-zf));

    uint16_t q = (uint16_t)(qf * 65535.0f);

    return q;
}

static inline void scp_md_prob(struct scp_stream *ss)
{
    int64_t d_ms = 0;

    if (ss->rtt_base > 0 && ss->srtt > 0) {
        d_ms =
            (int64_t)ss->srtt -
            (int64_t)ss->rtt_base;

        if (d_ms < 0)
            d_ms = 0;
    }

    /*
     * d is signed Q16.16, so its largest integral part
     * is 32767 ms.
     */
    if (d_ms > (INT32_MAX >> 16))
        d_ms = INT32_MAX >> 16;

    int32_t d = (int32_t)(d_ms * 65536LL);
    ss->d = d;

    /*
     * Before the first successful probe, gamma=beta=0 and q=0.5.
     * Probe mode owns cwnd, so the neutral probability cannot drive
     * the normal controller.
     */
    int64_t z64 =
        (int64_t)ss->prob_gamma_q16 +
        (((int64_t)ss->prob_beta_q16 * d) >> 16);

    if (z64 > INT32_MAX)
        z64 = INT32_MAX;
    if (z64 < INT32_MIN)
        z64 = INT32_MIN;

    ss->z = (int32_t)z64;
    ss->cong_q = logistic(ss->z);

    if (ss->cong_q_ema == 0) {
        ss->cong_q_ema = ss->cong_q;
    } else {
        ss->cong_q_ema =
            (uint16_t)(
                ((uint32_t)ss->cong_q_ema * 7U +
                 (uint32_t)ss->cong_q) >> 3
            );
    }
}

static uint32_t scp_prob_epoch(struct scp_stream *ss)
{
    uint32_t epoch;

    if (ss->srtt > UINT32_MAX / 2U)
        epoch = UINT32_MAX;
    else
        epoch = ss->srtt * 2U;

    if (epoch < SCP_RTO_MIN)
        epoch = SCP_RTO_MIN;

    return epoch;
}

static uint32_t scp_prob_delay_ms(struct scp_stream *ss)
{
    if (ss->d <= 0)
        return 0;

    return (uint32_t)(ss->d >> 16);
}

static uint32_t scp_abs_diff_u32(uint32_t a, uint32_t b)
{
    return a > b ? a - b : b - a;
}

static int scp_prob_refit(struct scp_stream *ss,
                          uint32_t d_low,
                          uint32_t d_high)
{
    uint32_t span;
    int32_t beta;
    int64_t gamma;

    if (d_high <= d_low)
        return SCP_ERR_GENERIC;

    span = d_high - d_low;

    /*
     * logit(0.6) in Q16 is approximately +26573;
     * logit(0.4) is approximately -26573.
     *
     * Round beta upward so d_high always reaches at least q=0.6,
     * then anchor d_low exactly at q=0.4.
     */
    beta =
        (int32_t)((53146U + span - 1U) / span);
    if (beta <= 0)
        beta = 1;

    gamma =
        -26573LL -
        (int64_t)beta * (int64_t)d_low;

    if (gamma > INT32_MAX)
        gamma = INT32_MAX;
    if (gamma < INT32_MIN)
        gamma = INT32_MIN;

    ss->prob_beta_q16 = beta;
    ss->prob_gamma_q16 = (int32_t)gamma;

    /* Old q history belongs to the old mapping. */
    ss->cong_q = 0;
    ss->cong_q_ema = 0;
    ss->z = 0;

    return 0;
}

static void scp_prob_start_probe(struct scp_stream *ss)
{
    ss->prob_mode = SCP_PROB_PROBE_DRAIN;

    ss->prob_next_time = 0;
    ss->prob_d_low = 0;
    ss->prob_d_prev = 0;
    ss->prob_noise = 0;

    ss->prob_stable_cnt = 0;
    ss->prob_rise_cnt = 0;
    ss->prob_samples = 0;
    ss->prob_seen_high = 0;

    ss->cc_phase = SCP_CC_PHASE_INC;
}

static void scp_prob_probe_step(struct scp_stream *ss)
{
    uint32_t now;
    uint32_t d_ms;
    uint32_t epoch;

    if (ss->srtt == 0 || ss->rtt_base == 0)
        return;

    now = scp_now_time();

    if (ss->prob_next_time &&
        SEQ_LT(now, ss->prob_next_time)) {
        return;
    }

    epoch = scp_prob_epoch(ss);
    ss->prob_next_time = now + epoch;

    d_ms = scp_prob_delay_ms(ss);

    if (ss->prob_mode == SCP_PROB_PROBE_DRAIN) {
        if (ss->cwnd > MSS) {
            uint32_t next =
                (uint32_t)(((uint64_t)ss->cwnd * 3U) / 4U);

            if (next < MSS)
                next = MSS;

            ss->cwnd = next;
            ss->prob_samples = 0;
            ss->prob_stable_cnt = 0;
            ss->prob_d_prev = d_ms;
            return;
        }

        /* At MSS, wait for the effective delay to settle. */
        if (ss->prob_samples == 0) {
            ss->prob_d_prev = d_ms;
            ss->prob_samples = 1;
            return;
        }

        {
            uint32_t diff =
                scp_abs_diff_u32(d_ms, ss->prob_d_prev);
            uint32_t noise =
                ss->rttvar ? ss->rttvar : 1U;

            if (diff <= noise)
                ss->prob_stable_cnt++;
            else
                ss->prob_stable_cnt = 0;

            ss->prob_d_prev = d_ms;
            ss->prob_samples++;

            if (ss->prob_stable_cnt <
                SCP_PROB_STABLE_EPOCHS) {
                return;
            }

            ss->prob_d_low = d_ms;
            ss->prob_noise = noise;
        }

        ss->prob_mode = SCP_PROB_PROBE_UP;
        ss->prob_rise_cnt = 0;
        ss->prob_samples = 0;
        ss->cwnd = 2U * MSS;

        if (ss->cwnd > CWND_WIN_MAX)
            ss->cwnd = CWND_WIN_MAX;

        return;
    }

    if (ss->prob_mode == SCP_PROB_PROBE_UP) {
        uint32_t noise =
            ss->prob_noise ? ss->prob_noise : 1U;
        uint32_t rise =
            noise > UINT32_MAX / 2U ?
            UINT32_MAX :
            noise * 2U;
        uint32_t threshold =
            ss->prob_d_low > UINT32_MAX - rise ?
            UINT32_MAX :
            ss->prob_d_low + rise;

        /* Hold this cwnd until the delay rise is confirmed. */
        if (d_ms >= threshold) {
            ss->prob_rise_cnt++;

            if (ss->prob_rise_cnt <
                SCP_PROB_RISE_EPOCHS) {
                return;
            }

            if (scp_prob_refit(ss,
                               ss->prob_d_low,
                               d_ms) == 0) {
                ss->prob_mode = SCP_PROB_NORMAL;
                ss->cc_phase = SCP_CC_PHASE_DEC;
                ss->prob_seen_high = 1;
                ss->prob_last_cycle = now;
                ss->prob_next_time = 0;
            }

            return;
        }

        ss->prob_rise_cnt = 0;

        if (ss->cwnd >= CWND_WIN_MAX) {
            /*
             * No RTT response was identifiable. Keep an older valid
             * mapping when one exists and retry after the normal
             * reprobe interval. With no old mapping, return safely to
             * MSS and let q remain neutral until the next probe.
             */
            if (ss->prob_beta_q16 == 0)
                ss->cwnd = MSS;

            ss->prob_mode = SCP_PROB_NORMAL;
            ss->cc_phase = SCP_CC_PHASE_INC;
            ss->prob_last_cycle = now;
            ss->prob_next_time = 0;
            return;
        }

        {
            uint32_t add = ss->cwnd / 4U;

            if (add < MSS)
                add = MSS;

            if (add > CWND_WIN_MAX - ss->cwnd)
                ss->cwnd = CWND_WIN_MAX;
            else
                ss->cwnd += add;
        }
    }
}

static int scp_prob_backlogged(struct scp_stream *ss)
{
    uint32_t flight =
        ss->snd_nxt - ss->snd_una;
    int has_unsent =
        SEQ_GT(ss->snd_seq_q, ss->snd_nxt);
    int window_busy =
        flight >= (ss->cwnd * 3U) / 4U;

    return ss->snd_wnd > ss->cwnd &&
           (has_unsent || window_busy);
}

static void scp_update_cwnd_fast(struct scp_stream *ss,
                                 uint32_t acked)
{
    const float q_inc_star = SCP_Q_INC_STAR;
    const float q_dec_star = SCP_Q_DEC_STAR;
    const float k_dec = 0.3f;

    float q_inst =
        (float)ss->cong_q / 65535.0f;

    float q_ema =
        (float)ss->cong_q_ema / 65535.0f;

    float w = (float)ss->cwnd;

    /*
     * Schmitt-trigger hysteresis:
     *
     * INC continues until instantaneous q reaches 0.6.
     * DEC continues until instantaneous q falls to 0.4.
     *
     * Inside (0.4, 0.6), retain the previous phase.
     */
    if (ss->cc_phase == SCP_CC_PHASE_INC) {
        if (ss->cong_q >= SCP_Q_HIGH_U16) {
            ss->cc_phase = SCP_CC_PHASE_DEC;
            ss->prob_seen_high = 1;
        }
    } else {
        if (ss->cong_q <= SCP_Q_LOW_U16) {
            ss->cc_phase = SCP_CC_PHASE_INC;

            if (ss->prob_seen_high) {
                ss->prob_last_cycle = scp_now_time();
                ss->prob_seen_high = 0;
            }
        }
    }

    if (ss->cc_phase == SCP_CC_PHASE_INC) {
        /*
         * Increase-branch virtual zero:
         *
         * q_inc_star = 9/13 ~= 0.6923 > q_high.
         */
        float x = q_inst / q_inc_star;

        if (x < 0.0f)
            x = 0.0f;
        if (x > 1.0f)
            x = 1.0f;

        {
            float weight = 1.0f - x * x;
            float denom = w > 1.0f ? w : 1.0f;
            float add =
                weight *
                ((float)MSS * (float)acked / denom);

            if (add < 1.0f)
                add = 1.0f;

            w += add;
        }
    } else {
        float q_dec = q_ema;

        if (q_dec < q_inst)
            q_dec = q_inst;

        {
            float x =
                (q_dec - q_dec_star) /
                (1.0f - q_dec_star);

            if (x < 0.0f)
                x = 0.0f;
            if (x > 1.0f)
                x = 1.0f;

            {
                float g = x * x;
                float excess = w - (float)MSS;
                float acked_ratio;
                float dec;

                if (excess < 0.0f)
                    excess = 0.0f;

                acked_ratio =
                    (w > 0.0f) ?
                    ((float)acked / w) :
                    0.0f;

                if (acked_ratio > 1.0f)
                    acked_ratio = 1.0f;

                dec =
                    k_dec *
                    g *
                    excess *
                    acked_ratio;

                if (dec > excess)
                    dec = excess;

                w -= dec;
            }
        }
    }

    if (w < (float)MSS)
        w = (float)MSS;

    if (w > (float)CWND_WIN_MAX)
        w = (float)CWND_WIN_MAX;

    ss->cwnd = (uint32_t)w;
}

static void scp_prob_init(struct scp_stream *ss)
{
    ss->cwnd = MSS;

    ss->sent_cnt = 0;
    ss->loss_cnt = 0;

    ss->p = 0;
    ss->p_ema = 0;

    ss->d = 0;
    ss->z = 0;

    ss->cong_q = 0;
    ss->cong_q_ema = 0;

    ss->cc_phase = SCP_CC_PHASE_INC;

    ss->ssthresh = 0;
    ss->recover_seq = 0;
    ss->fr_active = 0;

    ss->prob_gamma_q16 = 0;
    ss->prob_beta_q16 = 0;
    ss->prob_last_cycle = 0;

    scp_prob_start_probe(ss);
}

static void scp_prob_on_ack(struct scp_stream *ss,
                            uint32_t acked)
{
    uint32_t now;

    scp_md_prob(ss);

    if (ss->prob_mode != SCP_PROB_NORMAL) {
        scp_prob_probe_step(ss);
        return;
    }

    now = scp_now_time();

    if (scp_prob_backlogged(ss) &&
        now - ss->prob_last_cycle >=
            SCP_PROB_REPROBE_MS) {
        scp_prob_start_probe(ss);
        scp_prob_probe_step(ss);
        return;
    }

    scp_update_cwnd_fast(ss, acked);
}

static void scp_prob_on_loss(struct scp_stream *ss)
{

    (void)ss;
}

static void scp_prob_on_timeout(struct scp_stream *ss)
{
    (void)ss;
}

static uint32_t scp_prob_on_rtt(struct scp_stream *ss)
{
    uint32_t loss_rtt;
    uint32_t rtt;
    loss_rtt = (ss->p_ema * SCP_RTO_MIN) >> 16;
    rtt = ss->rtt_sample + loss_rtt;
    return rtt;
}

static void scp_aimd_init(struct scp_stream *ss)
{
    ss->cwnd = MTU;
    ss->ssthresh = CWND_WIN_MAX;

    ss->dup_acks = 0;
    ss->fr_active = 0;
    ss->recover_seq = 0;
    ss->cong_q = 0;
    ss->cong_q_ema = 0;
    ss->cc_phase = SCP_CC_PHASE_INC;
}

static void scp_aimd_on_ack(struct scp_stream *ss,
                            uint32_t acked)
{
    if (acked == 0)
        return;

    uint64_t next_cwnd;

    if (ss->cwnd < ss->ssthresh) {
        next_cwnd =
            (uint64_t)ss->cwnd +
            (uint64_t)acked;
    } else {
        uint64_t numerator =
            (uint64_t)MSS *
            (uint64_t)acked;

        uint32_t inc =
            (uint32_t)(
                numerator /
                (ss->cwnd ? ss->cwnd : 1U)
            );

        if (inc == 0)
            inc = 1;

        next_cwnd =
            (uint64_t)ss->cwnd +
            inc;
    }

    if (next_cwnd > CWND_WIN_MAX)
        next_cwnd = CWND_WIN_MAX;

    if (next_cwnd < MSS)
        next_cwnd = MSS;

    ss->cwnd = (uint32_t)next_cwnd;
}

static void scp_aimd_on_loss(struct scp_stream *ss)
{
    uint32_t flight =
        ss->snd_nxt -
        ss->snd_una;

    uint32_t half =
        flight / 2U;

    uint32_t minimum =
        2U * MSS;

    ss->ssthresh =
        (half > minimum) ?
        half :
        minimum;

    ss->cwnd = ss->ssthresh;

    if (ss->cwnd > CWND_WIN_MAX)
        ss->cwnd = CWND_WIN_MAX;
}

static void scp_aimd_on_timeout(struct scp_stream *ss)
{
    uint32_t flight =
        ss->snd_nxt -
        ss->snd_una;

    uint32_t half =
        flight / 2U;

    uint32_t minimum =
        2U * MSS;

    ss->ssthresh =
        (half > minimum) ?
        half :
        minimum;

    ss->cwnd = MSS;
}

static uint32_t scp_aimd_on_rtt(struct scp_stream *ss)
{
    return ss->rtt_sample;
}

static int scp_install_cc(struct scp_stream *ss,
                          enum scp_cc_id cc_id)
{
    if (!ss)
        return SCP_ERR_GENERIC;

    switch (cc_id) {
    case SCP_CC_PROB:
        ss->cc_init       = scp_prob_init;
        ss->cc_on_ack     = scp_prob_on_ack;
        ss->cc_on_loss    = scp_prob_on_loss;
        ss->cc_on_timeout = scp_prob_on_timeout;
        ss->cc_on_rtt     = scp_prob_on_rtt;
        break;

    case SCP_CC_AIMD:
        ss->cc_init       = scp_aimd_init;
        ss->cc_on_ack     = scp_aimd_on_ack;
        ss->cc_on_loss    = scp_aimd_on_loss;
        ss->cc_on_timeout = scp_aimd_on_timeout;
        ss->cc_on_rtt     = scp_aimd_on_rtt;
        break;

    default:
        return SCP_ERR_GENERIC;
    }

    ss->cc_id = (uint8_t)cc_id;

    if (ss->cc_init)
        ss->cc_init(ss);

    return 0;
}


int scp_set_cc(int fd, enum scp_cc_id cc_id)
{
    struct scp_stream *ss =
        hashmap_get(&scp_stream_map,
                    (void *)(uintptr_t)fd);

    if (!ss)
        return SCP_ERR_GENERIC;

    if (ss->state != SCP_CLOSED)
        return SCP_ERR_GENERIC;

    return scp_install_cc(ss, cc_id);
}