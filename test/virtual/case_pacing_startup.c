#include <assert.h>
#include "../../scp.c"

uint32_t scp_now_time(void)
{
    return 0;
}

int main(void)
{
    struct scp_stream ss = {0};

    ss.cwnd = MSS;
    ss.rto = 200;
    assert(scp_pacing_time(&ss, MSS) == 1);
    ss.rto = 2000;
    assert(scp_pacing_time(&ss, MSS) == 1);
    ss.cwnd = 2 * MSS;
    assert(scp_pacing_time(&ss, MSS) == 1);

    ss.srtt = 100;
    assert(scp_pacing_time(&ss, MSS) == 50);
    ss.cwnd = MSS;
    assert(scp_pacing_time(&ss, MSS) == 100);
    assert(scp_pacing_time(&ss, 1) == 0);
    return 0;
}
