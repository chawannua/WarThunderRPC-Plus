/* wtrpc-watcher.c
 *
 * Tiny always-on companion for WarThunderRPC-Plus.
 *
 * Watches for the War Thunder game process and starts/stops the presence
 * app (wtrpc.exe --managed) alongside it. Designed to idle at well under
 * 1 MB of working set: no CRT string/heap functions, no console, a single
 * polling loop, and a handful of fixed-size buffers.
 *
 * Command line (all optional, used for testing / overrides):
 *   --game <exe name>     process name to watch for (default aces.exe)
 *   --child <path>        child executable to launch (default
 *                          "<watcher_dir>\wtrpc\wtrpc.exe")
 *   --interval <ms>       poll interval (default 3000)
 *   --grace <ms>          grace period before killing the child after the
 *                          game disappears (default 15000)
 *   --backoff-base <ms>   initial backoff delay after a crashing child,
 *                          doubling on each further crash (default 5000)
 *   --fast-exit-ms <ms>   a child exit (any code) that ran for less than
 *                          this long is treated as a failure and subject
 *                          to backoff, even if its exit code was 0
 *                          (default 10000)
 *   --stop                signal a running watcher instance to shut down
 *                          (and its child with it), then wait for it to
 *                          exit. Used by the uninstaller/upgrader.
 *
 * MUTEX_NAME / STOP_EVENT_NAME can be overridden at compile time (-D) so
 * the test suite can run against its own kernel object names instead of
 * colliding with a production instance. build.ps1 leaves them at their
 * defaults.
 */

#include <windows.h>
#include <tlhelp32.h>
#include <shellapi.h>
#include <stdarg.h>
#include <strsafe.h>

/* WTRPC_TEST_ID lets the test harness give this build its own kernel
 * object names (via a plain alphanumeric -D token, so it survives
 * Windows PowerShell's native-argument quoting unscathed) instead of
 * having to pass a whole quoted wide-string literal on the command line.
 * MUTEX_NAME / STOP_EVENT_NAME can still be overridden directly too. */
#ifdef WTRPC_TEST_ID
#define WTRPC_STRINGIZE2(x) #x
#define WTRPC_STRINGIZE(x) WTRPC_STRINGIZE2(x)
#define WTRPC_WIDEN2(s) L##s
#define WTRPC_WIDEN(s) WTRPC_WIDEN2(s)
#define WTRPC_WSTR(x) WTRPC_WIDEN(WTRPC_STRINGIZE(x))
#define MUTEX_NAME (L"WarThunderRPC-Plus-Test-" WTRPC_WSTR(WTRPC_TEST_ID) L"-Mutex")
#define STOP_EVENT_NAME (L"WarThunderRPC-Plus-Test-" WTRPC_WSTR(WTRPC_TEST_ID) L"-Stop")
#endif

#ifndef MUTEX_NAME
#define MUTEX_NAME L"Local\\WarThunderRPC-Plus-Watcher-Mutex"
#endif
#ifndef STOP_EVENT_NAME
#define STOP_EVENT_NAME L"Local\\WarThunderRPC-Plus-Watcher-StopEvent"
#endif

#define BACKOFF_BASE_DEFAULT_MS 5000u
#define BACKOFF_MAX_MULTIPLIER 12u /* default: 5000 * 12 = 60000 */
#define LONG_RUN_MS (2u * 60u * 1000u)
#define LOG_CAP_BYTES (256u * 1024u)
#define FAST_EXIT_DEFAULT_MS 10000u

static wchar_t g_gameExe[MAX_PATH] = L"aces.exe";
static wchar_t g_childPath[MAX_PATH * 2] = L"";
static DWORD g_intervalMs = 3000;
static DWORD g_graceMs = 15000;
static DWORD g_backoffBaseMs = BACKOFF_BASE_DEFAULT_MS;
static DWORD g_fastExitMs = FAST_EXIT_DEFAULT_MS;
static wchar_t g_logPath[MAX_PATH * 2] = L"";

static HANDLE g_job = NULL;
static HANDLE g_childProcess = NULL;
static ULONGLONG g_childStartTick = 0;
static DWORD g_backoffMs = 0;
static ULONGLONG g_backoffUntilTick = 0;
static BOOL g_gameGoneTracking = FALSE;
static ULONGLONG g_gameGoneSinceTick = 0;

/* ---------------------------------------------------------------------- */
/* Logging: best-effort, size-capped, never fatal if it fails.            */
/* ---------------------------------------------------------------------- */

static void LogLine(const wchar_t *fmt, ...)
{
    if (g_logPath[0] == L'\0')
        return;

    /* Bounded formatting: unlike wvsprintfW/wsprintfW (which can write up
     * to 1024 wchars regardless of the destination size), StringCchVPrintfW
     * / StringCchPrintfW never write past the buffer, truncating instead. */
    wchar_t msg[512];
    va_list args;
    va_start(args, fmt);
    StringCchVPrintfW(msg, ARRAYSIZE(msg), fmt, args);
    va_end(args);

    wchar_t line[560];
    StringCchPrintfW(line, ARRAYSIZE(line), L"%s\r\n", msg);

    /* GENERIC_WRITE (not just FILE_APPEND_DATA) so SetEndOfFile is allowed
     * to actually truncate the file when the size cap is exceeded. */
    HANDLE h = CreateFileW(g_logPath, GENERIC_WRITE, FILE_SHARE_READ, NULL,
                            OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE)
        return;

    LARGE_INTEGER size;
    if (GetFileSizeEx(h, &size) && (ULONGLONG)size.QuadPart > LOG_CAP_BYTES) {
        SetFilePointer(h, 0, NULL, FILE_BEGIN);
        SetEndOfFile(h);
    }
    /* GENERIC_WRITE has no auto-append semantics, so always seek to end
     * (which is offset 0 right after a truncation above) before writing. */
    SetFilePointer(h, 0, NULL, FILE_END);

    /* Write UTF-8 (no BOM) instead of raw UTF-16LE so the log is readable
     * in plain-text viewers like Notepad. */
    char utf8[2048];
    int wlen = lstrlenW(line);
    int utf8Len = WideCharToMultiByte(CP_UTF8, 0, line, wlen, utf8,
                                       (int)sizeof(utf8), NULL, NULL);
    if (utf8Len > 0) {
        DWORD written;
        WriteFile(h, utf8, (DWORD)utf8Len, &written, NULL);
    }
    CloseHandle(h);
}

/* ---------------------------------------------------------------------- */
/* Process discovery                                                      */
/* ---------------------------------------------------------------------- */

static BOOL IsProcessRunning(const wchar_t *exeName)
{
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE)
        return FALSE;

    PROCESSENTRY32W entry;
    entry.dwSize = sizeof(entry);
    BOOL found = FALSE;

    if (Process32FirstW(snap, &entry)) {
        do {
            if (lstrcmpiW(entry.szExeFile, exeName) == 0) {
                found = TRUE;
                break;
            }
        } while (Process32NextW(snap, &entry));
    }

    CloseHandle(snap);
    return found;
}

/* ---------------------------------------------------------------------- */
/* Child process management                                               */
/* ---------------------------------------------------------------------- */

static void CloseChildHandle(void)
{
    if (g_childProcess != NULL) {
        CloseHandle(g_childProcess);
        g_childProcess = NULL;
    }
}

static void LaunchChild(void)
{
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    wchar_t cmdLine[MAX_PATH * 2 + 32];
    wsprintfW(cmdLine, L"\"%s\" --managed", g_childPath);

    BOOL ok = CreateProcessW(
        g_childPath, cmdLine, NULL, NULL, FALSE,
        CREATE_NO_WINDOW | CREATE_SUSPENDED, NULL, NULL, &si, &pi);

    if (!ok) {
        LogLine(L"launch failed, error=%d", (int)GetLastError());
        return;
    }

    if (g_job != NULL) {
        if (!AssignProcessToJobObject(g_job, pi.hProcess)) {
            LogLine(L"job assignment failed, error=%d", (int)GetLastError());
        }
    }

    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);

    g_childProcess = pi.hProcess;
    g_childStartTick = GetTickCount64();
    LogLine(L"child launched, pid=%d", (int)pi.dwProcessId);
}

static void TerminateChildNow(void)
{
    if (g_childProcess == NULL)
        return;
    LogLine(L"terminating child");
    TerminateProcess(g_childProcess, 1);
    WaitForSingleObject(g_childProcess, 5000);
    CloseChildHandle();
    g_gameGoneTracking = FALSE;
    g_gameGoneSinceTick = 0;
}

/* ---------------------------------------------------------------------- */
/* Main tick                                                              */
/* ---------------------------------------------------------------------- */

static void Tick(void)
{
    ULONGLONG now = GetTickCount64();
    BOOL gameRunning = IsProcessRunning(g_gameExe);

    /* Reap the child if it has exited. */
    if (g_childProcess != NULL) {
        DWORD wait = WaitForSingleObject(g_childProcess, 0);
        if (wait == WAIT_OBJECT_0) {
            DWORD exitCode = 1;
            GetExitCodeProcess(g_childProcess, &exitCode);
            ULONGLONG ranMs = now - g_childStartTick;
            CloseChildHandle();
            LogLine(L"child exited, code=%d, ranMs=%I64u", (int)exitCode, ranMs);

            /* A clean exit (code 0) that happened almost immediately is
             * treated as a failure too, so a child that is broken in a way
             * that makes it exit 0 right away can't relaunch-storm the
             * watcher. Only a code-0 exit after running for a while is
             * considered a genuine, non-penalized shutdown. */
            BOOL isFailure = (exitCode != 0) || (ranMs < g_fastExitMs);

            if (gameRunning) {
                if (!isFailure) {
                    g_backoffMs = 0;
                    g_backoffUntilTick = 0;
                } else {
                    DWORD backoffMax = g_backoffBaseMs * BACKOFF_MAX_MULTIPLIER;
                    if (ranMs > LONG_RUN_MS) {
                        g_backoffMs = g_backoffBaseMs;
                    } else if (g_backoffMs == 0) {
                        g_backoffMs = g_backoffBaseMs;
                    } else {
                        g_backoffMs *= 2;
                        if (g_backoffMs > backoffMax)
                            g_backoffMs = backoffMax;
                    }
                    g_backoffUntilTick = now + g_backoffMs;
                    LogLine(L"backing off %d ms", (int)g_backoffMs);
                }
            } else {
                g_backoffMs = 0;
                g_backoffUntilTick = 0;
            }
        }
    }

    if (gameRunning) {
        g_gameGoneTracking = FALSE;
        g_gameGoneSinceTick = 0;

        if (g_childProcess == NULL) {
            if (g_backoffUntilTick == 0 || now >= g_backoffUntilTick) {
                LaunchChild();
            }
        }
    } else {
        g_backoffMs = 0;
        g_backoffUntilTick = 0;

        if (g_childProcess != NULL) {
            if (!g_gameGoneTracking) {
                g_gameGoneTracking = TRUE;
                g_gameGoneSinceTick = now;
            } else if (now - g_gameGoneSinceTick >= g_graceMs) {
                TerminateChildNow();
            }
        }
    }
}

/* ---------------------------------------------------------------------- */
/* Argument parsing                                                       */
/* ---------------------------------------------------------------------- */

static BOOL ArgEquals(const wchar_t *arg, const wchar_t *name)
{
    return lstrcmpiW(arg, name) == 0;
}

static DWORD ParseUInt(const wchar_t *s, DWORD fallback)
{
    if (s == NULL || s[0] == L'\0')
        return fallback;
    DWORD v = 0;
    for (const wchar_t *p = s; *p; ++p) {
        if (*p < L'0' || *p > L'9')
            return fallback;
        v = v * 10 + (DWORD)(*p - L'0');
    }
    return v;
}

static void DefaultChildPath(void)
{
    wchar_t selfPath[MAX_PATH];
    GetModuleFileNameW(NULL, selfPath, MAX_PATH);

    wchar_t *lastSlash = NULL;
    for (wchar_t *p = selfPath; *p; ++p) {
        if (*p == L'\\')
            lastSlash = p;
    }
    if (lastSlash != NULL)
        *lastSlash = L'\0';

    wsprintfW(g_childPath, L"%s\\wtrpc\\wtrpc.exe", selfPath);
}

static void DefaultLogPath(void)
{
    wchar_t appData[MAX_PATH];
    DWORD n = GetEnvironmentVariableW(L"APPDATA", appData, MAX_PATH);
    if (n == 0 || n >= MAX_PATH)
        return;

    wchar_t dir[MAX_PATH * 2];
    wsprintfW(dir, L"%s\\WarThunderRPC-Plus", appData);
    CreateDirectoryW(dir, NULL); /* best-effort; fine if it already exists */

    wsprintfW(g_logPath, L"%s\\watcher.log", dir);
}

static BOOL g_stopRequested = FALSE;

static void ParseArgs(int argc, wchar_t **argv)
{
    for (int i = 1; i < argc; ++i) {
        if (ArgEquals(argv[i], L"--game") && i + 1 < argc) {
            lstrcpynW(g_gameExe, argv[++i], MAX_PATH);
        } else if (ArgEquals(argv[i], L"--child") && i + 1 < argc) {
            lstrcpynW(g_childPath, argv[++i], MAX_PATH * 2);
        } else if (ArgEquals(argv[i], L"--interval") && i + 1 < argc) {
            g_intervalMs = ParseUInt(argv[++i], g_intervalMs);
        } else if (ArgEquals(argv[i], L"--grace") && i + 1 < argc) {
            g_graceMs = ParseUInt(argv[++i], g_graceMs);
        } else if (ArgEquals(argv[i], L"--backoff-base") && i + 1 < argc) {
            g_backoffBaseMs = ParseUInt(argv[++i], g_backoffBaseMs);
        } else if (ArgEquals(argv[i], L"--fast-exit-ms") && i + 1 < argc) {
            g_fastExitMs = ParseUInt(argv[++i], g_fastExitMs);
        } else if (ArgEquals(argv[i], L"--stop")) {
            g_stopRequested = TRUE;
        }
    }
}

/* ---------------------------------------------------------------------- */
/* --stop: signal a running instance and wait for it to shut down.        */
/* ---------------------------------------------------------------------- */

static int RunStop(void)
{
    HANDLE hStop = OpenEventW(EVENT_MODIFY_STATE, FALSE, STOP_EVENT_NAME);
    if (hStop == NULL)
        return 0; /* nothing running */

    SetEvent(hStop);
    CloseHandle(hStop);

    HANDLE hMutex = OpenMutexW(SYNCHRONIZE, FALSE, MUTEX_NAME);
    if (hMutex != NULL) {
        WaitForSingleObject(hMutex, 15000);
        CloseHandle(hMutex);
    }
    return 0;
}

/* ---------------------------------------------------------------------- */
/* Entry point                                                            */
/* ---------------------------------------------------------------------- */

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance,
                     PWSTR pCmdLine, int nCmdShow)
{
    (void)hInstance;
    (void)hPrevInstance;
    (void)pCmdLine;
    (void)nCmdShow;

    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);

    DefaultChildPath();
    DefaultLogPath();

    if (argv != NULL) {
        ParseArgs(argc, argv);
        LocalFree(argv);
    }

    if (g_stopRequested)
        return RunStop();

    HANDLE hMutex = CreateMutexW(NULL, TRUE, MUTEX_NAME);
    if (hMutex == NULL)
        return 1;
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        CloseHandle(hMutex);
        return 0; /* another instance owns it */
    }

    HANDLE hStopEvent = CreateEventW(NULL, TRUE, FALSE, STOP_EVENT_NAME);

    g_job = CreateJobObjectW(NULL, NULL);
    if (g_job != NULL) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION info;
        ZeroMemory(&info, sizeof(info));
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(g_job, JobObjectExtendedLimitInformation,
                                 &info, sizeof(info));
    }

    LogLine(L"watcher started, game=%s, child=%s, interval=%d, grace=%d, backoffBase=%d, fastExitMs=%d",
            g_gameExe, g_childPath, (int)g_intervalMs, (int)g_graceMs, (int)g_backoffBaseMs,
            (int)g_fastExitMs);

    for (;;) {
        DWORD wait = (hStopEvent != NULL)
                          ? WaitForSingleObject(hStopEvent, g_intervalMs)
                          : (Sleep(g_intervalMs), WAIT_TIMEOUT);
        if (wait == WAIT_OBJECT_0) {
            LogLine(L"stop requested");
            break;
        }
        Tick();
    }

    if (g_childProcess != NULL)
        TerminateChildNow();

    if (g_job != NULL)
        CloseHandle(g_job); /* kills anything still assigned, belt and braces */

    if (hStopEvent != NULL)
        CloseHandle(hStopEvent);

    LogLine(L"watcher exiting");

    ReleaseMutex(hMutex);
    CloseHandle(hMutex);
    return 0;
}
