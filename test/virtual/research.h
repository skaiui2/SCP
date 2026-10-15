#ifndef SCP_RESEARCH_H
#define SCP_RESEARCH_H
#include "vnet.h"
int research_send(void *user, const void *buf, size_t len);
int research_finish(uint32_t end_ms);
void research_observe(unsigned direction, int arrival, const void *buf, size_t len, uint32_t now);
extern uint32_t research_feedback_min_ms, research_feedback_max_ms;
extern uint64_t research_feedback_count;
extern uint64_t research_payload_packets[MSS + 1], research_nonfinal_short_packets;
extern uint32_t research_last_new_payload_bytes;
extern uint32_t research_last_data_ms, research_new_bytes;
extern uint64_t research_new_packets, research_retrans_packets, research_retrans_bytes;
extern unsigned research_errors;
#endif
