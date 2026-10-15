#ifndef VNET_H
#define VNET_H
#include <stdint.h>
#include <stddef.h>
struct transfer;
void vnet_attach(struct transfer *a, struct transfer *b);
struct vnet_endpoint { unsigned direction; };
typedef void (*vnet_observer)(unsigned direction, int arrival, const void *buf, size_t len, uint32_t now);
void vnet_observe(vnet_observer callback);
void vnet_init(uint32_t delay_ms, uint32_t mbps, uint32_t loss_ppm, uint32_t seed);
int vnet_send(void *user, const void *buf, size_t len);
int vnet_step(void);
void vnet_free(void);
extern uint64_t vnet_packets, vnet_dropped;
#endif
