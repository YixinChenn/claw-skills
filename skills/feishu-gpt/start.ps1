param(
    [switch]$LocalChat
)

# 飞书 × ChatGPT 机器人 快速启动脚本
$WindowTitle = "飞书 × ChatGPT 机器人"
$Host.UI.RawUI.WindowTitle = $WindowTitle

function Start-WindowTitleKeeper {
    param([string]$Title)

    $timer = New-Object System.Timers.Timer
    $timer.Interval = 1000
    $timer.AutoReset = $true
    $handler = [System.Timers.ElapsedEventHandler]{
        param($Sender, $EventArgs)
        try {
            [Console]::Title = $Title
        } catch {
        }
    }
    $timer.add_Elapsed($handler)
    $timer.Start()
    return @{ Timer = $timer; Handler = $handler }
}

function Stop-WindowTitleKeeper {
    param($Keeper)

    if (-not $Keeper) {
        return
    }
    try {
        $Keeper.Timer.Stop()
        $Keeper.Timer.Dispose()
    } catch {
    }
    try {
        $Keeper.Timer.remove_Elapsed($Keeper.Handler)
    } catch {
    }
}

$windowTitleKeeper = Start-WindowTitleKeeper -Title $WindowTitle

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  飞书 × ChatGPT 机器人 启动中..." -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# 检查 Python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[错误] 未找到 Python，请先安装：https://www.python.org/downloads/" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
Write-Host "[OK] Python 已就绪" -ForegroundColor Green

# 检查并安装依赖
$depsInstalled = python -c "import lark_oapi; import openai" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[*] 正在安装依赖 lark-oapi / openai ..." -ForegroundColor Yellow
    pip install lark-oapi openai
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 依赖安装失败，请检查网络" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
}
Write-Host "[OK] 依赖已就绪" -ForegroundColor Green

# 检查配置是否存在
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configDir = Join-Path $scriptDir "app_config"
$configPath = Join-Path $configDir "local.py"
$botPath = Join-Path $scriptDir "bot.py"

if (-not (Test-Path $configPath)) {
    Write-Host "[错误] 未找到 app_config\\local.py，请先从 app_config\\local.example.py 复制并填写配置" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

# 检查 APP_ID 是否已填写
$configContent = Get-Content $configPath -Raw
if ($configContent -match 'cli_xxxxxx') {
    Write-Host ""
    Write-Host "[提示] 配置文件中的 APP_ID 还未修改！" -ForegroundColor Yellow
    Write-Host "       请填写 app_config\\local.py 中的飞书 APP_ID / APP_SECRET / OPENAI_API_KEY" -ForegroundColor Yellow
    Read-Host "按回车退出"
    exit 1
}

Write-Host ""
if ($configContent -match 'sk-xxxxxxxx') {
    Write-Host ""
    Write-Host "[提示] app_config\\local.py 中的 OPENAI_API_KEY 还未修改！" -ForegroundColor Yellow
    Read-Host "按回车退出"
    exit 1
}

$cliEnabled = $configContent -match 'FEISHU_CLI_ENABLED\s*=\s*True'
Write-Host "[*] FEISHU_CLI_ENABLED = $cliEnabled" -ForegroundColor Gray
Write-Host "[*] 配置来源 = $configPath" -ForegroundColor Gray

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  所有检查通过，启动机器人！" -ForegroundColor Green
if ($LocalChat) {
    Write-Host "  将仅启动本地对话模式" -ForegroundColor Gray
} else {
    Write-Host "  将同时启动飞书机器人和本地对话" -ForegroundColor Gray
}
Write-Host "  关闭此窗口即可停止机器人" -ForegroundColor Gray
Write-Host "  仅本地对话可运行: python bot.py --local-chat" -ForegroundColor Gray
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

if ($LocalChat) {
    try {
        python $botPath --local-chat
    } finally {
        Stop-WindowTitleKeeper $windowTitleKeeper
    }
} else {
    try {
        python $botPath
    } finally {
        Stop-WindowTitleKeeper $windowTitleKeeper
    }
}

Write-Host ""
Write-Host "[机器人已停止]" -ForegroundColor Yellow
Read-Host "按回车退出"
