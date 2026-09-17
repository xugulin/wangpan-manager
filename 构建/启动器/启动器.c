/* 网盘管理 · Windows 启动器
 * ============================================================================
 * 为什么需要它：
 *   1. 双击就能用 —— 用户不需要装 Python，也不用敲命令；
 *   2. **绝不弹控制台黑窗口** —— 本程序用 GUI 子系统编译（-mwindows），
 *      再用 CREATE_NO_WINDOW 拉起项目自带的 pythonw.exe（它本身也是 GUI 子系统，
 *      不会分配控制台），所以不会出现"黑窗口一闪一闪"的情况；
 *   3. 支持把文件/文件夹拖到图标上启动，参数原样透传给 启动.py。
 *
 * 交叉编译（Linux 上编 Windows 版）：
 *   x86_64-w64-mingw32-gcc -O2 -municode -mwindows -o 启动.exe 启动器.c
 * ============================================================================
 */
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <wchar.h>

#define 路径长 MAX_PATH
#define 命令长 32768

/* 解释器候选：绿色版可能把依赖装在 venv 里，也可能直接装在自带的独立 Python 里 */
static const wchar_t *解释器候选[] = {
    L"\\运行环境\\venv\\Scripts\\pythonw.exe",
    L"\\运行环境\\python\\pythonw.exe",
    L"\\运行环境\\venv\\Scripts\\python.exe",
    L"\\运行环境\\python\\python.exe",
};

static int 文件存在(const wchar_t *路径) {
    DWORD 属性 = GetFileAttributesW(路径);
    return 属性 != INVALID_FILE_ATTRIBUTES && !(属性 & FILE_ATTRIBUTE_DIRECTORY);
}

int wmain(void) {
    wchar_t 根[路径长] = {0};
    if (!GetModuleFileNameW(NULL, 根, 路径长)) return 1;
    wchar_t *斜杠 = wcsrchr(根, L'\\');
    if (斜杠) *斜杠 = 0;

    wchar_t 解释器[路径长] = {0};
    for (int i = 0; i < (int)(sizeof(解释器候选) / sizeof(解释器候选[0])); i++) {
        wchar_t 试[路径长] = {0};
        _snwprintf(试, 路径长 - 1, L"%s%s", 根, 解释器候选[i]);
        if (文件存在(试)) { wcsncpy(解释器, 试, 路径长 - 1); break; }
    }
    if (解释器[0] == 0) {
        MessageBoxW(NULL,
            L"找不到自带的 Python 解释器。\n\n"
            L"请确认压缩包已经**完整解压**到普通文件夹后再运行\n"
            L"（不要直接在压缩包里双击，也不要只解压一部分）。\n\n"
            L"期望位置：运行环境\\venv\\Scripts\\pythonw.exe",
            L"网盘管理 · 启动失败", MB_ICONERROR | MB_OK);
        return 1;
    }

    wchar_t 脚本[路径长] = {0};
    _snwprintf(脚本, 路径长 - 1, L"%s\\启动.py", 根);
    if (!文件存在(脚本)) {
        MessageBoxW(NULL, L"找不到 启动.py，压缩包可能不完整。",
                    L"网盘管理 · 启动失败", MB_ICONERROR | MB_OK);
        return 1;
    }

    /* 命令行 = "解释器" "脚本" + 启动器收到的参数（拖进来的文件也在这里） */
    static wchar_t 命令行[命令长];
    int 已有 = _snwprintf(命令行, 命令长 - 1, L"\"%s\" \"%s\"", 解释器, 脚本);
    if (已有 < 0) 已有 = 0;

    const wchar_t *尾巴 = GetCommandLineW();
    if (*尾巴 == L'"') {                      /* 跳过带引号的 argv[0] */
        尾巴++;
        while (*尾巴 && *尾巴 != L'"') 尾巴++;
        if (*尾巴) 尾巴++;
    } else {                                  /* 跳过不带引号的 argv[0] */
        while (*尾巴 && *尾巴 != L' ' && *尾巴 != L'\t') 尾巴++;
    }
    if (*尾巴) {
        wcsncat(命令行, 尾巴, 命令长 - 1 - wcslen(命令行));
    }

    STARTUPINFOW 启动信息;
    PROCESS_INFORMATION 进程信息;
    ZeroMemory(&启动信息, sizeof(启动信息));
    启动信息.cb = sizeof(启动信息);
    启动信息.dwFlags = STARTF_USESHOWWINDOW;
    启动信息.wShowWindow = SW_SHOWNORMAL;
    ZeroMemory(&进程信息, sizeof(进程信息));

    if (!CreateProcessW(解释器, 命令行, NULL, NULL, FALSE,
                        CREATE_NO_WINDOW,      /* 关键：不给子进程分配控制台 */
                        NULL, 根, &启动信息, &进程信息)) {
        MessageBoxW(NULL, L"启动失败：无法创建进程。", L"网盘管理", MB_ICONERROR | MB_OK);
        return 2;
    }
    CloseHandle(进程信息.hProcess);
    CloseHandle(进程信息.hThread);
    return 0;
}
