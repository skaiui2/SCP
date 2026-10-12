#ifndef SCP_RESEARCH_H
#define SCP_RESEARCH_H
#include "vnet.h"
int research_send(void *user, const void *buf, size_t len);
int research_finish(uint32_t end_ms);
extern uint32_t research_last_data_ms, research_new_bytes;
extern uint64_t research_new_packets, research_retrans_packets, research_retrans_bytes;
extern unsigned research_errors;
#endif
