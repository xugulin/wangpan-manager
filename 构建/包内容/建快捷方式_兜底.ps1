# 网盘管理 · 桌面快捷方式兜底（PowerShell + IShellLinkW）
# ============================================================================
# 谁调它：包里的「创建桌面图标.vbs」在 WSH/ShellLinkObject 都建不出中文目标的
#         .lnk 时，会把本文件复制到 %TEMP%（纯 ASCII 路径）再执行 ——
#         命令行里**不出现任何中文**，中文路径通过 %TEMP%\wangpan-shortcut.txt
#         （UTF-16）传进来，所以不受控制台代码页影响。
#
# 为什么还要这一步（真机 windows-latest 实测，2026-09-21）：
#   * `WScript.Shell.CreateShortcut(...).TargetPath = <中文路径>`
#     → `5 Invalid procedure call or argument`，Save 却"成功"，留下没有目标的空壳 .lnk
#     （WSH 内部按系统 ANSI 代码页处理路径：英文版 Windows 上中文必挂）；
#   * `Shell.Application` 的 ShellLinkObject 能**读能写**中文 Path，但 Save 不落地
#     （真机 err=438）。
#   于是兜底走 Shell32 的原生 Unicode 接口：IShellLinkW + IPersistFile。
#   PowerShell 的执行策略**不拦 `-File` + `-ExecutionPolicy Bypass`**，
#   而且这里的代码是内联 C#（Add-Type），不依赖任何外部模块。
#
# 参数：无。输入文件 %TEMP%\wangpan-shortcut.txt（UTF-16LE，三行）：
#       第 1 行 = 要生成的 .lnk 完整路径（可以含中文）
#       第 2 行 = 快捷方式目标（启动器 exe/bat 的完整路径，可以含中文）
#       第 3 行 = 工作目录（包目录）
# 输出：stdout 一行 `[OK] ...` 或 `[!] ...`，退出码 0/1。
# ============================================================================
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$cfg = Join-Path $env:TEMP 'wangpan-shortcut.txt'
if (-not (Test-Path -LiteralPath $cfg)) {
    Write-Output '[!] missing config file'
    exit 1
}
$行 = Get-Content -LiteralPath $cfg -Encoding Unicode
if ($行.Count -lt 3) {
    Write-Output '[!] config file needs 3 lines'
    exit 1
}
$lnkPath = $行[0].Trim()
$target = $行[1].Trim()
$workDir = $行[2].Trim()
if (-not $lnkPath -or -not $target) {
    Write-Output '[!] empty lnkPath/target'
    exit 1
}
if (-not (Test-Path -LiteralPath $target)) {
    Write-Output "[!] target not found: $target"
    exit 1
}

$源码 = @'
using System;
using System.Runtime.InteropServices;
using System.Text;

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
internal class ShellLinkCoClass { }

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
 Guid("000214F9-0000-0000-C000-000000000046")]
internal interface IShellLinkW
{
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile,
                 int cch, IntPtr pfd, int fFlags);
    void GetIDList(out IntPtr ppidl);
    void SetIDList(IntPtr pidl);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cch);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cch);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cch);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
    void GetHotkey(out short pwHotkey);
    void SetHotkey(short wHotkey);
    void GetShowCmd(out int piShowCmd);
    void SetShowCmd(int iShowCmd);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath,
                         int cch, out int piIcon);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, int dwReserved);
    void Resolve(IntPtr hwnd, int fFlags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
}

[ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IPersistFile
{
    void GetClassID(out Guid pClassID);
    [PreserveSig] int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, int dwMode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, bool fRemember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
    void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
}

public static class WangpanLnk
{
    public static void Create(string lnkPath, string target, string workDir, string desc)
    {
        var link = (IShellLinkW)new ShellLinkCoClass();
        link.SetPath(target);
        if (!string.IsNullOrEmpty(workDir)) link.SetWorkingDirectory(workDir);
        link.SetIconLocation(target, 0);
        if (!string.IsNullOrEmpty(desc)) link.SetDescription(desc);
        ((IPersistFile)link).Save(lnkPath, false);
    }

    // 读回目标路径：只判断"文件存在"会把没有目标的空壳 .lnk 当成成功（踩过）
    public static string ReadTarget(string lnkPath)
    {
        var link = (IShellLinkW)new ShellLinkCoClass();
        ((IPersistFile)link).Load(lnkPath, 0);
        var sb = new StringBuilder(1024);
        link.GetPath(sb, sb.Capacity, IntPtr.Zero, 0);
        return sb.ToString();
    }
}
'@

try {
    if (-not ('WangpanLnk' -as [type])) {
        Add-Type -TypeDefinition $源码 -Language CSharp | Out-Null
    }
    $desktop = [Environment]::GetFolderPath('Desktop')
    $dir = Split-Path -Parent $lnkPath
    if (-not (Test-Path -LiteralPath $dir)) { $dir = $desktop }
    $tmp = Join-Path $env:TEMP 'wangpan-manager-shortcut.lnk'
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    [WangpanLnk]::Create($tmp, $target, $workDir, '网盘管家：百度 / 夸克 / 光鸭，一个界面全搞定')
    $读回 = [WangpanLnk]::ReadTarget($tmp)
    if ($读回 -ne $target) {
        Write-Output "[!] read back mismatch: [$读回]"
        exit 1
    }
    if (Test-Path -LiteralPath $lnkPath) { Remove-Item -LiteralPath $lnkPath -Force }
    Move-Item -LiteralPath $tmp -Destination $lnkPath -Force
    if (-not (Test-Path -LiteralPath $lnkPath)) {
        Write-Output '[!] lnk not found after move'
        exit 1
    }
    Write-Output "[OK] desktop shortcut created: $lnkPath"
    Write-Output "     target: $target"
    exit 0
} catch {
    Write-Output "[!] $($_.Exception.Message)"
    exit 1
}
