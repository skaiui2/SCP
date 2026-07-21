#ifndef SCP_H
#define SCP_H

#include <stdint.h>
#include <stddef.h>
#include "link_list.h"
#include "hashmap.h"
#include "queue.h"
#include "in_cksum.h"
#include "rbtree.h"

typedef void (*scp_timer_cb_t)(void *arg);
typedef void *scp_timer_handle_t;

struct scp_timer {
    struct rb_node node;
    uint32_t expire;   
    uint32_t timeout;
    scp_timer_cb_t cb; 
    void *arg;
    uint8_t active; //If node in tree, set it. 
};

//Set by yourself.
#define SCP_SB  1
#define SCP_DEBUG 0
#define SCP_DUMP 1
#define SCP_RUN_DEBUG 1

#define RETRANS_COUNT_MAX 1000
#define SCP_RTO_MIN 200
#define SCP_RTO_MAX 2000
#define RETRANS_RECO_MAX 1
#define RETRANS_GAP_MAX  4
#define SCP_RECV_LIMIT (1 << 24)
#define SEND_WIN_INIT (1 << 24)
#define RECV_WIN_INIT (1 << 24)
#define CWND_WIN_MAX  (1 << 17)
#define MTU 1460
#define PERSIST_INTERVAL 200
#define MAX_IDLE_FAIL  3
#define IDLE_TIMEOUT 100000
#define MSS (MTU - sizeof(struct scp_hdr))
#define P_WND  32
#define SEND_MEM_MAX  (1 << 30)
#define RECV_MEM_MAX  (1 << 30)

struct scp_transport_class {
    int (*send)(void *user, const void *buf, size_t len);
    int (*recv)(void *user, void *buf, size_t maxlen);
    int (*close)(void *user);
    void *user;
};

struct scp_buf {
    union { 
        struct list_node list; 
        struct rb_node rb; 
    };
    size_t len;
    uint32_t seq; // save it from hdr.
    uint8_t sent:1;
    uint8_t dir:1;
    uint8_t *data;
    uint32_t payload_off;
};

struct scp_hdr {
    uint32_t seq;
    uint32_t ack;
    uint32_t sack; //first hollow
    uint32_t fd;
    uint32_t wnd;
    uint16_t len;
    uint16_t cksum;
    uint16_t flags;
    uint32_t time_val;
} __attribute__((packed));

#define SCP_FLAG_DATA  0x01
#define SCP_FLAG_ACK   0x02
#define SCP_FLAG_RST   0x04
#define SCP_FLAG_CONNECT  0x08
#define SCP_FLAG_PING  0x10
#define SCP_FLAG_RESEND 0x20
#define SCP_FLAG_CONNECT_ACK 0x40
#define SCP_FLAG_FIN         0x80 

#define SCP_DIR_INPUT 1
#define SCP_DIR_SEND 0

#define SCP_ERR_GENERIC   -1 
#define SCP_ERR_NOBUF     -2  
#define SCP_ERR_NOMEM     -3 


enum {
    SCP_CLOSED,
    SCP_SYN_SENT,
    SCP_SYN_RECV,
    SCP_ESTABLISHED,
    SCP_FIN_WAIT,
    SCP_LAST_ACK,
};

struct scp_stream {
    struct list_node node;              // linked into global stream list
    struct scp_transport_class *st_class; // transport callbacks (send/recv)

    struct scp_timer t_retrans;         // retransmission timer
    struct scp_timer t_persist;         // persist / keepalive timer
    struct scp_timer t_hs;              // handshake retry timer
    struct scp_timer t_fin;             // FIN retry timer

    uint32_t dst_fd;                    // remote endpoint id
    uint32_t src_fd;                    // local endpoint id

    uint32_t snd_una;                   // first unacknowledged seq
    uint32_t snd_seq_q;                 // next seq to enqueue
    uint32_t snd_nxt;                   // highest seq ever sent
    uint32_t snd_wnd;                   // peer's advertised window

    uint32_t rcv_nxt;                   // next seq we expect
    uint32_t rcv_wnd;                   // our advertised window

    uint32_t snd_wmem;                  // send buffer watermark
    uint32_t rcv_wmem;                  // recv buffer watermark

    uint32_t packet_bytes;              //output bytes
    uint32_t packet_count; 

    uint8_t  rtt_updated;
    uint32_t rtt_sample;
    uint32_t srtt;                      // smoothed RTT
    uint32_t rtt_base;
    uint32_t rttvar;                    // RTT variation
    uint32_t rto;                       // retransmission timeout
    uint32_t rto_recovery;

    uint8_t  timeout_count;             // consecutive timeout counter

    struct list_node snd_q;             // send queue (in-order)

    struct rb_root rcv_buf_q;           // out-of-order receive tree
    struct list_node rcv_data_q;        // in-order delivered data
    uint32_t sb_hiwat;                  // receive buffer limit
    uint32_t sb_cc;                     // current receive buffer usage

    uint32_t idle_failures;             // idle timeout counter
    uint32_t last_active;               // last activity timestamp

    uint32_t timestamp;                 // local timestamp
    int state;                          // connection state
    uint32_t iss;                       // initial send seq
    uint32_t irs;                       // initial recv seq
    uint8_t retry;                      // handshake/FIN retry count

    uint32_t cwnd;                      // congestion window
    uint8_t dup_acks;                   // duplicate ACK counter

    uint8_t fr_active;
    uint32_t last_gap_rexmit_ack;

    uint32_t sent_cnt;
    uint32_t loss_cnt;
    uint32_t p_ema;  
    uint32_t p;
    
    int32_t  d;
    int32_t  z;
    uint16_t cong_q;
    uint16_t cong_q_ema;
    
    void (*cc_init)(struct scp_stream *ss);
    void (*cc_on_ack)(struct scp_stream *ss);
    void (*cc_on_loss)(struct scp_stream *ss);
    void (*cc_on_timeout)(struct scp_stream *ss);
};

/*
 * Greater than:
 */
#define SEQ_GT(a,b)  ((int32_t)((a) - (b)) > 0)
#define SEQ_GEQ(a,b) ((int32_t)((a) - (b)) >= 0)

#define SEQ_EQ(a,b) ((int32_t)((a) == (b)))
/*
 * less than:
*/
#define SEQ_LT(a,b)  ((int32_t)((a) - (b)) < 0)
#define SEQ_LEQ(a,b) ((int32_t)((a) - (b)) <= 0)


#define min(a, b) ((a) < (b) ? (a) : (b))
#define imin(a, b) ((int)(a) < (int)(b) ? (int)(a) : (int)(b))


int scp_init(size_t max_streams);
struct scp_stream *scp_stream_alloc(struct scp_transport_class *st_class, int src_fd, int dst_fd);
int scp_stream_free(struct scp_stream *ss);

int scp_connect(int fd);
int scp_input(void *ctx, void *buf, size_t len);
int scp_send(int fd, void *buf, size_t len);
int scp_recv(int fd, void *buf, size_t len);
void scp_close(int fd);

void scp_timer_process(void);

#endif