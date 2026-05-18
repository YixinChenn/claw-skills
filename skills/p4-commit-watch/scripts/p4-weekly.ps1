# p4-weekly.ps1
# 分析多个仓库里组员的过去 7 天提交，生成周报汇总
# 用法: pwsh -File p4-weekly.ps1 -ConfigFile <path> [-HoursBack 168]

param(
    [string]$ConfigFile = "$PSScriptRoot\..\p4-watch-config.json",
    [int]$HoursBack = 0
)

chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# ---------- 加载配置 ----------
if (-not (Test-Path $ConfigFile)) {
    Write-Host "[ERROR] Config file not found: $ConfigFile"
    exit 1
}

$config  = Get-Content $ConfigFile -Encoding UTF8 | ConvertFrom-Json
$members = $config.team_watch.members
$repos   = $config.repos
$hoursBack = if ($HoursBack -gt 0) { $HoursBack } else { $config.weekly_watch.hours_back }

# ---------- 中文名映射 ----------
$nameMap = @{
    "lianlian"     = "莲莲"
    "yuechu"       = "月初"
    "zengtiantong" = "天同"
    "tianqi"       = "天麒"
    "lilichen"     = "粒粒尘"
    "bourne"       = "芒果"
    "summer"       = "Summer"
    "yukunlong"    = "阿龙"
    "jingyuan002"  = "景元"
}
function Get-DisplayName($user) {
    if ($nameMap.ContainsKey($user)) { return $nameMap[$user] } else { return $user }
}

if (-not $config.weekly_watch.enabled) {
    Write-Host "[INFO] weekly_watch disabled in config."
    exit 0
}

if ($members.Count -eq 0) {
    Write-Host "[WARN] No team members configured in team_watch.members"
    exit 0
}

$since = (Get-Date).AddHours(-$hoursBack).ToString("yyyy/MM/dd:HH:mm:ss")

# ---------- 逐人逐仓库拉取 ----------
$teamReport = @{}

foreach ($member in $members) {
    $allCommits = @()

    foreach ($repo in $repos) {
        $stream = $repo.stream
        $label  = if ($repo.label) { $repo.label } else { $repo.name }

        $rawChanges = @(p4 changes -s submitted -t -u $member "$stream@$since,@now" 2>&1)
        $changes = @($rawChanges | Where-Object { $_ -match "^Change" })
        if ($changes.Count -eq 0) { continue }

        foreach ($line in $changes) {
            if ($line -notmatch "^Change (\d+) on (\d{4}/\d{2}/\d{2}) (\d{2}:\d{2}:\d{2}) by (\S+) '(.+)") { continue }
            $clNum  = $Matches[1]
            $clDate = $Matches[2]
            $clTime = $Matches[3]

            $descLines = @(p4 describe -s $clNum 2>&1)
            $fullDesc = ($descLines | Select-Object -Skip 1 | Where-Object { $_ -match "^\t" } |
                         Select-Object -First 5 | ForEach-Object { $_.TrimStart("`t") }) -join " | "

            $fileSection = $false
            $files = @()
            foreach ($dl in $descLines) {
                if ($dl -match "^Affected files") { $fileSection = $true; continue }
                if ($dl -match "^Differences") { break }
                if ($fileSection -and $dl -match "^\.\.\. (//\S+)#(\d+) (\w+)") {
                    $files += [PSCustomObject]@{ Path=$Matches[1]; Rev=$Matches[2]; Action=$Matches[3] }
                }
            }

            $fileCount = $files.Count
            $cpp   = @($files | Where-Object { $_.Path -match "\.(cpp|h|inl)$" }).Count
            $bp    = @($files | Where-Object { $_.Path -match "\.(uasset|umap)$" }).Count
            $lua   = @($files | Where-Object { $_.Path -match "\.lua$" }).Count
            $other = $fileCount - $cpp - $bp - $lua

            $tags = [regex]::Matches($fullDesc, '\[([^\]]+)\]') | ForEach-Object { $_.Groups[1].Value }
            $tagStr = if ($tags.Count -gt 0) { $tags -join ", " } else { "none" }

            $diffTmpFile = ""
            $codeFiles = @($files | Where-Object { $_.Path -match "\.(cpp|h|inl|lua)$" })
            if ($codeFiles.Count -gt 0) {
                $rawDiff = @(p4 describe -du $clNum 2>&1)
                $diffStart = ($rawDiff | Select-String -Pattern "^Differences" | Select-Object -First 1).LineNumber
                # 动态行数上限：基础 500 行，每多一个代码文件加 150 行，上限 3000
                $diffLineLimit = [Math]::Min(500 + $codeFiles.Count * 150, 3000)
                $diffContent = if ($diffStart) {
                    ($rawDiff | Select-Object -Skip $diffStart | Select-Object -First $diffLineLimit) -join "`n"
                } else {
                    ($rawDiff | Select-Object -First $diffLineLimit) -join "`n"
                }

                # 对 [add] 操作的 Lua 文件，p4 describe -du 不会展开内容，用 p4 print 补充
                $addedLuaFiles = @($codeFiles | Where-Object { $_.Path -match "\.lua$" -and $_.Action -eq "add" })
                if ($addedLuaFiles.Count -gt 0) {
                    $extraContent = @()
                    foreach ($lf in $addedLuaFiles) {
                        $fileRef = "$($lf.Path)#$($lf.Rev)"
                        $extraContent += "`n==== $fileRef (text/new) ===="
                        $printLines = @(p4 print -q $fileRef 2>&1 | Select-Object -First 200)
                        $extraContent += ($printLines -join "`n")
                    }
                    $diffContent += "`n" + ($extraContent -join "`n")
                }

                $diffTmpFile = [System.IO.Path]::GetTempFileName() + ".txt"
                [System.IO.File]::WriteAllText($diffTmpFile, $diffContent, [System.Text.Encoding]::UTF8)
            }

            $allCommits += [PSCustomObject]@{
                Repo        = $label
                CL          = $clNum
                DateTime    = "$clDate $clTime"
                FullDesc    = $fullDesc
                Tags        = $tagStr
                FileCount   = $fileCount
                CPP         = $cpp
                BP          = $bp
                Lua         = $lua
                Other       = $other
                Files       = $files
                DiffTmpFile = $diffTmpFile
            }
        }
    }

    $teamReport[$member] = $allCommits
}

# ---------- 生成报告 ----------
$today   = (Get-Date).ToString("yyyy/MM/dd")
$period  = "$((Get-Date).AddHours(-$hoursBack).ToString('MM/dd HH:mm')) ~ $((Get-Date).ToString('MM/dd HH:mm'))"
$repoLabels = ($repos | ForEach-Object { if ($_.label) { $_.label } else { $_.name } }) -join ", "
$totalCLs = ($teamReport.Values | ForEach-Object { $_.Count } | Measure-Object -Sum).Sum

$lines = @()
$lines += "Team P4 Weekly Report - $today"
$lines += "Repos: $repoLabels"
$lines += "Period: $period"
$lines += "Members: $($members.Count) | Total Commits: $totalCLs"
$lines += ""

foreach ($member in $members) {
    $commits = @($teamReport[$member])
    $displayName = Get-DisplayName $member
    $lines += "========================"
    $lines += "[$displayName]  $($commits.Count) commit(s)"

    if ($commits.Count -eq 0) {
        $lines += "  (no commits this week)"
        $lines += ""
        continue
    }

    $totalFiles = ($commits | ForEach-Object { $_.FileCount } | Measure-Object -Sum).Sum
    $totalCPP   = ($commits | ForEach-Object { $_.CPP }       | Measure-Object -Sum).Sum
    $totalBP    = ($commits | ForEach-Object { $_.BP }        | Measure-Object -Sum).Sum
    $totalLua   = ($commits | ForEach-Object { $_.Lua }       | Measure-Object -Sum).Sum

    $lines += "  Summary: $totalFiles files (C++/H: $totalCPP | BP: $totalBP | Lua: $totalLua)"
    $lines += ""

    foreach ($c in $commits) {
        $lines += "  [$($c.Repo)] CL $($c.CL)  $($c.DateTime)"
        $lines += "  DESC: $($c.FullDesc)"
        $lines += "  TAGS: $($c.Tags) | $($c.FileCount) files (C++: $($c.CPP) | BP: $($c.BP) | Lua: $($c.Lua) | Other: $($c.Other))"
        foreach ($f in $c.Files) {
            $short = $f.Path -replace "^//root/[^/]+/[^/]+/", ""
            $lines += "    [$($f.Action)] $short"
        }
        if ($c.DiffTmpFile) { $lines += "  DIFF_FILE: $($c.DiffTmpFile)" }
        $lines += ""
    }
}

$report = $lines -join "`n"
Write-Host $report

# 保存本地备份
$logDir = Join-Path $PSScriptRoot "..\reports"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "$((Get-Date).ToString('yyyy-MM-dd'))-weekly.txt"
$report | Out-File -FilePath $logFile -Encoding utf8
Write-Host "Report saved: $logFile"

# 输出所有 diff 路径供 AI 读取（逗号分隔）
$allDiffPaths = @()
foreach ($member in $members) {
    foreach ($c in @($teamReport[$member])) {
        if ($c.DiffTmpFile) { $allDiffPaths += $c.DiffTmpFile }
    }
}
if ($allDiffPaths.Count -gt 0) {
    Write-Host "DIFF_PATHS:$($allDiffPaths -join ',')"
}
