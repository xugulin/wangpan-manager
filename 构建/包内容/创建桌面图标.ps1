# 网盘管理 · 创建桌面快捷方式（PowerShell 版，处理中文最可靠）
#
# 为什么要单独一个 .ps1：
#   cmd.exe 会按控制台代码页把 .bat 当**字节**解析，`if exist "中文.exe"` 这种
#   判断在中文字体/代码页下会失配（Wine 与真实 Windows 上都实测过），
#   而 PowerShell 是 Unicode 原生的：文件名、快捷方式名都不会乱。
#
# 用法：双击 创建桌面图标.bat（它只是转调本脚本）

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$候选 = @('启动.exe', '启动.bat', '启动.py')
$目标 = $null
foreach ($名 in $候选) {
    $试 = Join-Path $here $名
    if (Test-Path -LiteralPath $试) { $目标 = $试; break }
}

if (-not $目标) {
    Write-Host "[X] 找不到 启动.exe / 启动.bat / 启动.py"
    Write-Host "    目录：$here"
    Write-Host "    请把压缩包完整解压到普通文件夹后再运行。"
    exit 1
}

$桌面 = [Environment]::GetFolderPath('Desktop')
$名 = [string]([char]0x7F51 + [char]0x76D8 + [char]0x7BA1 + [char]0x7406)
$lnk路径 = Join-Path $桌面 ($名 + '.lnk')

try {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($lnk路径)
    $lnk.TargetPath = $目标
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation = "$目标,0"
    $lnk.Description = '网盘管家：百度 / 夸克 / 光鸭，一个界面全搞定'
    $lnk.Save()
    Write-Host "[OK] 已在桌面创建快捷方式：$lnk路径"
    Write-Host "     目标：$目标"
} catch {
    Write-Host "[!] 创建快捷方式失败：$($_.Exception.Message)"
    Write-Host "    也可以直接把「启动.exe」（或 启动.bat）拖到桌面当快捷方式。"
    exit 1
}
