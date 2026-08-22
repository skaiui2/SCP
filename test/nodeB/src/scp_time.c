#include <stdint.h>
#include <stdio.h>
#include <time.h>

#include "scp_time.h"

uint32_t scp_clock = 0;

static struct timespec scp_clock_start;

int scp_time_init(void)
{
    if (clock_gettime(CLOCK_MONOTONIC, &scp_clock_start) != 0) {
        perror("clock_gettime(CLOCK_MONOTONIC)");
        return -1;
    }

    scp_clock = 0;
    return 0;
}

uint32_t scp_now_time(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        perror("clock_gettime(CLOCK_MONOTONIC)");
        return scp_clock;
    }

    int64_t sec =
        (int64_t)now.tv_sec -
        (int64_t)scp_clock_start.tv_sec;

    int64_t nsec =
        (int64_t)now.tv_nsec -
        (int64_t)scp_clock_start.tv_nsec;

    if (nsec < 0) {
        sec--;
        nsec += 1000000000LL;
    }

    uint64_t elapsed_ms =
        (uint64_t)sec * 1000ULL +
        (uint64_t)nsec / 1000000ULL;

    scp_clock = (uint32_t)elapsed_ms;

    return scp_clock;
}