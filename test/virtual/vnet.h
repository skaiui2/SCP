#ifndef VNET_H
#define VNET_H
#include <stdint.h>
#include <stddef.h>
struct vnet_endpoint { unsigned direction; };
void vnet_init(uint32_t delay_ms, uint32_t mbps, uint32_t loss_ppm, uint32_t seed);
int vnet_send(void *user, const void *buf, size_t len);
int vnet_step(void);
void vnet_free(void);
extern uint64_t vnet_packets, vnet_dropped;
#endif
