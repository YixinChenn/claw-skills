param(
    [string]$Title = "Agent task finished",
    [string]$Message = "",
    [ValidateSet("info", "success", "failed", "error", "timeout")]
    [string]$Status = "success",
    [string]$Source = "local-agent",
    [string]$JobId = "",
    [string]$ConversationId = "",
    [string]$Cwd = (Get-Location).Path,
    [string]$Url = "",
    [string]$Dir = ""
)

$ErrorActionPreference = "Stop"

function Resolve-AgentConversationId([string]$CurrentValue, [string]$SourceValue) {
    if (-not [string]::IsNullOrWhiteSpace($CurrentValue) -and $CurrentValue.Trim().ToLowerInvariant() -notin @("unknown", "last", "latest")) {
        return $CurrentValue.Trim()
    }

    $envNames = @("AGENT_CONVERSATION_ID")
    if ($SourceValue -match "(?i)codex") {
        $envNames += @("CODEX_THREAD_ID", "CODEX_CONVERSATION_ID")
    }
    elseif ($SourceValue -match "(?i)claude") {
        $envNames += "CLAUDE_SESSION_ID"
    }
    foreach ($envName in $envNames) {
        $envValue = [Environment]::GetEnvironmentVariable($envName)
        if (-not [string]::IsNullOrWhiteSpace($envValue) -and $envValue.Trim().ToLowerInvariant() -notin @("unknown", "last", "latest")) {
            return $envValue.Trim()
        }
    }
    # The newest local session may belong to another concurrent chat.
    return ""
}

if ([string]::IsNullOrWhiteSpace($Dir)) {
    $Dir = Join-Path $PSScriptRoot "runtime_data\agent_notify"
}

New-Item -ItemType Directory -Force -Path $Dir | Out-Null

$ConversationId = Resolve-AgentConversationId $ConversationId $Source

$payload = [ordered]@{
    title = $Title
    message = $Message
    status = $Status
    source = $Source
    job_id = $JobId
    conversation_id = $ConversationId
    cwd = $Cwd
    url = $Url
    created_at = (Get-Date).ToString("s")
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$name = "notify_{0}_{1}.json" -f $stamp, ([guid]::NewGuid().ToString("N").Substring(0, 8))
$tempPath = Join-Path $Dir ($name + ".tmp")
$finalPath = Join-Path $Dir $name
$json = $payload | ConvertTo-Json -Depth 4
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($tempPath, $json + [Environment]::NewLine, $utf8NoBom)
Move-Item -LiteralPath $tempPath -Destination $finalPath -Force

Write-Output $finalPath
