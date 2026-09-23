/* fakechild.c
 *
 * Stand-in for wtrpc.exe used by watcher/tests/test_watcher.ps1.
 *
 * On every launch it appends a "start" line (recording its pid, whether
 * --managed was on the command line, and the current tick count) to the
 * log file named by the WTRPC_TEST_LOG environment variable, then reads a
 * one-word directive from the file named by WTRPC_TEST_CTL to decide what
 * to do:
 *
 *   exit0  - sleep briefly, then exit(0)   (simulates a clean match-end)
 *   exit1  - sleep briefly, then exit(1)   (simulates a crash)
 *   (else) - sleep for a long time, so the harness can test termination
 *            via grace-period timeout or job-object kill-on-close
 *
 * Before exiting via exit0/exit1 it appends an "exit" line to the log too,
 * so the harness can measure relaunch/backoff timing precisely.
 */

#include <windows.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
    char ctlPath[MAX_PATH] = "";
    char logPath[MAX_PATH] = "";
    GetEnvironmentVariableA("WTRPC_TEST_CTL", ctlPath, MAX_PATH);
    GetEnvironmentVariableA("WTRPC_TEST_LOG", logPath, MAX_PATH);
    if (ctlPath[0] == '\0' || logPath[0] == '\0')
        return 3;

    int hasManaged = 0;
    for (int i = 1; i < argc; ++i) {
        if (lstrcmpiA(argv[i], "--managed") == 0)
            hasManaged = 1;
    }

    FILE *lf = fopen(logPath, "a");
    if (lf != NULL) {
        fprintf(lf, "start pid=%lu managed=%d tick=%lu\n",
                (unsigned long)GetCurrentProcessId(), hasManaged,
                (unsigned long)GetTickCount());
        fclose(lf);
    }

    char mode[32] = "sleep";
    FILE *cf = fopen(ctlPath, "r");
    if (cf != NULL) {
        if (fgets(mode, sizeof(mode), cf) != NULL) {
            size_t n = strlen(mode);
            while (n > 0 && (mode[n - 1] == '\n' || mode[n - 1] == '\r'))
                mode[--n] = '\0';
        }
        fclose(cf);
    }

    int rc = 0;
    if (strcmp(mode, "exit0") == 0) {
        Sleep(150);
        rc = 0;
    } else if (strcmp(mode, "exit1") == 0) {
        Sleep(150);
        rc = 1;
    } else {
        Sleep(600000);
        rc = 0;
    }

    FILE *lf2 = fopen(logPath, "a");
    if (lf2 != NULL) {
        fprintf(lf2, "exit pid=%lu rc=%d tick=%lu\n",
                (unsigned long)GetCurrentProcessId(), rc,
                (unsigned long)GetTickCount());
        fclose(lf2);
    }

    return rc;
}
