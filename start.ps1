# 启动 cursor-float 悬浮窗（无控制台窗口）
#
#   .\start.ps1            正常启动
#   .\start.ps1 --demo     用假数据启动，检查外观
#
# 脚本会自动寻找一个带 tkinter 的 Python：优先用 DSH 自带的运行时，
# 其次用 PATH 里的 python / py 启动器。

param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ExtraArgs)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$entry = Join-Path $here 'cursor_float.py'

if (-not (Test-Path $entry)) { throw "找不到 $entry" }

function Get-PythonCandidates {
    $list = @()
    $runtimes = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes'
    if (Test-Path $runtimes) {
        $list += Get-ChildItem $runtimes -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'dependencies\python\python.exe' }
    }
    foreach ($n in 'python.exe', 'python3.exe') {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { $list += $c.Source }
    }
    $list
}

$python = $null
foreach ($cand in Get-PythonCandidates) {
    if (-not (Test-Path $cand)) { continue }
    & $cand -c 'import tkinter, sqlite3' 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $cand; break }
}

if (-not $python) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { $python = $launcher.Source }
}
if (-not $python) { throw '没有找到带 tkinter 的 Python。' }

# 用 pythonw 启动，避免留下控制台窗口
$pythonw = $python -replace '(?i)python\.exe$', 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

# 组装启动参数。
# 注意：不带任何额外参数时 $ExtraArgs 是 $null，而 @(x) + $null 会得到一个
# 含 $null 元素的数组，Start-Process 会以「参数为 Null」为由拒绝执行。
# 所以这里逐个过滤，而不是直接相加。
$argList = @("`"$entry`"")
if ($ExtraArgs) {
    foreach ($a in $ExtraArgs) {
        if ($null -ne $a -and "$a".Length -gt 0) { $argList += $a }
    }
}

Start-Process -FilePath $pythonw -ArgumentList $argList -WorkingDirectory $here
Write-Host "已启动 cursor-float（$pythonw）"
