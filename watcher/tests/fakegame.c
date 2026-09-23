/* fakegame.c
 *
 * Stand-in for aces.exe used by watcher/tests/test_watcher.ps1. Just sits
 * alive until the test harness kills it. Whatever the watcher is told is
 * the "game" process name (via --game), this binary just needs to exist
 * under that name while running.
 */

#include <windows.h>

int main(void)
{
    Sleep(600000); /* 10 minutes; the test harness kills us long before this */
    return 0;
}
