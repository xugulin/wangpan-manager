# 网盘管理 · Windows 自检（PowerShell 版）
#
# 为什么单独一个 .ps1：cmd 的 .bat 是按控制台代码页当字节解析的，中文路径/中文名
# 会乱码（实测 mkdir 出来的目录名变成 GBK 乱码）；PowerShell 是 Unicode 原生的。
#
# 它在自己机器上把能自动查的都查一遍，最后生成「自检报告」文件夹供发送给作者。
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$report = Join-Path $here '自检报告'
if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Path $report -Force | Out-Null
$log = Join-Path $report '自检结果.txt'

function Say([string]$text) {
    Write-Host $text
    Add-Content -LiteralPath $log -Value $text -Encoding UTF8
}

Say '==== 网盘管理 Windows 自检 ===='
Say ("时间：" + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
Say ("目录：" + $here)
Say ''

# ---------- 1) 系统信息 ----------
Say '[1] 系统信息'
Say ("  " + [System.Environment]::OSVersion.VersionString)
Say ("  PowerShell " + $PSVersionTable.PSVersion + "  64bit=" + [Environment]::Is64BitProcess)
try {
    Add-Type -AssemblyName System.Windows.Forms
    $s = [System.Windows.Forms.Screen]::PrimaryScreen
    Say ("  主屏 " + $s.Bounds.Width + "x" + $s.Bounds.Height + " 工作区 " + $s.WorkingArea.Width + "x" + $s.WorkingArea.Height)
} catch { Say "  (拿不到屏幕信息)" }
try {
    $lp = (Get-ItemProperty 'HKCU:\Control Panel\Desktop' -Name LogPixels -ErrorAction SilentlyContinue).LogPixels
    Say ("  LogPixels=" + $lp)
    $dpi = (Get-ItemProperty 'HKCU:\Control Panel\Desktop\WindowMetrics' -ErrorAction SilentlyContinue)
    Say ("  AppliedDPI=" + $dpi.AppliedDPI)
} catch { }
Say ''

# ---------- 2) 包内容 ----------
Say '[2] 包内容检查'
foreach ($n in @('启动.exe', '启动.bat', '启动.py', '使用说明.txt', '创建桌面图标.bat', '创建桌面图标.vbs', '运行环境')) {
    $p = Join-Path $here $n
    if (Test-Path -LiteralPath $p) { Say ("  OK   " + $n) } else { Say ("  MISS " + $n) }
}
Say ''

# ---------- 3) 解释器与依赖 ----------
Say '[3] 自带 Python 与依赖'
$py = $null
foreach ($c in @('运行环境\venv\Scripts\python.exe', '运行环境\python\python.exe')) {
    $p = Join-Path $here $c
    if (Test-Path -LiteralPath $p) { $py = $p; break }
}
if (-not $py) {
    Say '  MISS interpreter'
} else {
    Say ("  解释器：" + $py)
    try {
        $out = & $py -c "import sys,PySide6,httpx;print('python',sys.version.split()[0]);print('PySide6',PySide6.__version__);print('httpx',httpx.__version__)" 2>&1
        foreach ($l in $out) { Say ("  " + $l) }
    } catch { Say ("  依赖导入失败：" + $_.Exception.Message) }
}
Say ''

# ---------- 4) 创建桌面图标.bat 端到端 ----------
Say '[4] 创建桌面快捷方式（历史上这一步会乱码或失败）'
$desktop = [Environment]::GetFolderPath('Desktop')
try {
    Get-ChildItem -LiteralPath $desktop -Filter '*.lnk' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    Say '  先清掉桌面上已有的 .lnk（只为测试，别的文件不动）'
    $bat = Get-ChildItem -LiteralPath $here -Filter '*.bat' | Where-Object { $_.Name -notlike 'start*' } | Select-Object -First 1
    $target = Join-Path $here '创建桌面图标.bat'
    if (Test-Path -LiteralPath $target) {
        $out = & cmd.exe /c "`"$target`"" 2>&1 | Out-String
        foreach ($l in ($out -split "`r?`n")) { if ($l.Trim()) { Say ("  | " + $l) } }
    }
    $lnk = Get-ChildItem -LiteralPath $desktop -Filter '*.lnk' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($lnk) {
        Say ("  OK   桌面生成了快捷方式：" + $lnk.Name)
        if ($lnk.BaseName -match '[\u4e00-\u9fa5]') { Say '  名字里含中文：正常' } else { Say '  ⚠️ 名字里没有中文（期望「网盘管理」）' }
    } else {
        Say '  FAIL 桌面没有生成快捷方式'
    }
} catch { Say ("  出错：" + $_.Exception.Message) }
Say ''

# ---------- 5) 启动程序并抓日志 ----------
Say '[5] 启动程序（--性能诊断），30 秒后收集日志'
if ($py) {
    try {
        Start-Process -FilePath $py -ArgumentList @('启动.py', '--性能诊断') -WorkingDirectory $here | Out-Null
        Say '  已启动；如果你能看到窗口，请在 30 秒内点几下你觉得卡的地方'
        Start-Sleep -Seconds 30
        Get-Process -Name python, pythonw -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    } catch { Say ("  启动失败：" + $_.Exception.Message) }
}
Say ''

# ---------- 6) 收集日志 ----------
Say '[6] 收集日志'
foreach ($n in @('卡顿诊断.log', '一键启动.log', '界面日志.txt')) {
    $p = Join-Path (Join-Path $here '数据') $n
    if (Test-Path -LiteralPath $p) {
        Copy-Item -LiteralPath $p -Destination (Join-Path $report $n) -Force -ErrorAction SilentlyContinue
        Say ("  OK   " + $n)
    } else {
        Say ("  --   没有 " + $n)
    }
}
Say ''
Say '==== 自检结束 ===='
Say ("请把整个「自检报告」文件夹发给作者：" + $report)

if (Test-Path -LiteralPath $report) { Start-Process explorer.exe $report }
exit 0
