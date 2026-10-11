#include "transfer.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
#include <ctype.h>
int main(int argc, char **argv)
{
    int sender, cc = SCP_CC_AIMD, flows = 1;
    uint64_t bytes = UINT64_C(1073741824);
    uint32_t seconds = 0;
    uint64_t amount = 0;
    char *end;
    const char *mode = argc > 4 ? argv[4] : "aimd";
    if (argc < 2 || argc > 5 || (strcmp(argv[1], "A") && strcmp(argv[1], "B"))) goto usage;
    sender = !strcmp(argv[1], "A");
    if (argc > 3) {
        errno = 0;
        amount = strtoull(argv[3], &end, 10);
        if (errno || !isdigit((unsigned char)argv[3][0]) || *end || !amount) goto usage;
    }
    if (!strcmp(mode, "prob-prob") || !strcmp(mode, "prob-aimd")) {
        flows = 2; cc = SCP_CC_PROB;
        if (argc > 3 && amount > 86400) goto usage;
        seconds = argc > 3 ? (uint32_t)amount : 300;
    } else {
        if (!strcmp(mode, "prob")) cc = SCP_CC_PROB;
        else if (strcmp(mode, "aimd") && strcmp(mode, "pacing")) goto usage;
        if (argc > 3) bytes = amount;
        if (!bytes) goto usage;
    }
    setvbuf(stdout, NULL, _IONBF, 0);
    return transfer_udp_run(sender, argc > 2 ? argv[2] : "127.0.0.1", bytes,
                            cc, flows, seconds, !strcmp(mode, "prob-aimd"));
usage:
    fprintf(stderr, "usage: %s A|B [peer-ip] [bytes|seconds] [aimd|prob|pacing|prob-prob|prob-aimd]\n", argv[0]);
    return 2;
}
