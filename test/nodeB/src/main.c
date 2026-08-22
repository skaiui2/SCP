#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int test_prob_100m_main(void);
int test_aimd_100m_main(void);
int test_fairness_main(int argc, char **argv);
int test_pacing_main(void);

enum test_kind {
    TEST_PROB_100M,
    TEST_AIMD_100M,
    TEST_PACING,
    TEST_FAIRNESS
};

struct test_case {
    const char *name;
    enum test_kind kind;
};

static const struct test_case g_cases[] = {
    {"prob-100m", TEST_PROB_100M},
    {"aimd-100m", TEST_AIMD_100M},
    {"pacing", TEST_PACING},
    {"prob-prob", TEST_FAIRNESS},
    {"prob-aimd", TEST_FAIRNESS}
};

static int run_body(const struct test_case *tc, const char *duration)
{
    if (tc->kind == TEST_PROB_100M)
        return test_prob_100m_main();

    if (tc->kind == TEST_AIMD_100M)
        return test_aimd_100m_main();

    if (tc->kind == TEST_PACING)
        return test_pacing_main();

    {
        char *argv[] = {
            (char *)"nodeB",
            (char *)tc->name,
            (char *)duration,
            NULL
        };

        return test_fairness_main(3, argv);
    }
}

static int run_isolated(const struct test_case *tc, const char *duration)
{
    pid_t pid = fork();
    int status;

    if (pid < 0) {
        perror("fork");
        return 1;
    }

    if (pid == 0) {
        int rc = run_body(tc, duration);
        _exit(rc == 0 ? 0 : 1);
    }

    if (waitpid(pid, &status, 0) < 0) {
        perror("waitpid");
        return 1;
    }

    if (!WIFEXITED(status)) {
        fprintf(stderr, "%s terminated by signal %d\n",
                tc->name,
                WTERMSIG(status));
        return 1;
    }

    if (WEXITSTATUS(status) != 0) {
        fprintf(stderr, "%s failed with exit code %d\n",
                tc->name,
                WEXITSTATUS(status));
        return 1;
    }

    return 0;
}
int main(int argc, char **argv)
{
    const char *selection;
    const char *duration;
    size_t i;
    int matched = 0;
    int failures = 0;

    setvbuf(stdout, NULL, _IONBF, 0);

    if (argc < 2 || argc > 3) {
        fprintf(stderr,
                "usage: %s "
                "all|prob-100m|aimd-100m|pacing|prob-prob|prob-aimd "
                "[fairness-seconds]\n",
                argv[0]);
        return 1;
    }

    selection = argv[1];
    duration = argc == 3 ? argv[2] : "300";

    for (i = 0; i < sizeof(g_cases) / sizeof(g_cases[0]); ++i) {
        if (strcmp(selection, "all") != 0 &&
            strcmp(selection, g_cases[i].name) != 0)
            continue;

        matched = 1;
        printf("[runner] start %s\n", g_cases[i].name);

        if (run_isolated(&g_cases[i], duration) != 0) {
            printf("{\"type\":\"case\",\"name\":\"%s\","
                    "\"status\":\"failed\"}\n",
                    g_cases[i].name);

            failures++;

            if (strcmp(selection, "all") != 0)
                return 1;

            continue;
        }

        printf("[runner] finish %s\n", g_cases[i].name);
    }

    if (!matched) {
        fprintf(stderr, "unknown test: %s\n", selection);
        return 1;
    }

    return failures ? 1 : 0;
}
