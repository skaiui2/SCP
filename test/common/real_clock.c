#define _POSIX_C_SOURCE 200809L
#include <stdint.h>
#ifdef _WIN32
#include <windows.h>
uint32_t scp_now_time(void) { return (uint32_t)GetTickCount64(); }
#else
#include <time.h>
uint32_t scp_now_time(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint32_t)((uint64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000);
}
#endif
