param(
    [int]$AdminApiPort = 8000,
    [int]$DeviceGatewayPort = 8001,
    [int]$DashboardPort = 8501
)

$ErrorActionPreference = "Stop"
$tailscale = "C:\Program Files\Tailscale\tailscale.exe"

if (-not (Test-Path -LiteralPath $tailscale)) {
    throw "Tailscale is not installed at $tailscale"
}

function Invoke-TailscaleConfig {
    param([string[]]$Arguments)

    $id = [Guid]::NewGuid().ToString("N")
    $stdoutPath = Join-Path $env:TEMP "batnode-tailscale-$id.out"
    $stderrPath = Join-Path $env:TEMP "batnode-tailscale-$id.err"
    try {
        $process = Start-Process -FilePath $tailscale -ArgumentList $Arguments `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath `
            -WindowStyle Hidden -PassThru
        $finished = $process.WaitForExit(15000)
        if (-not $finished) {
            Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
        } else {
            $process.WaitForExit()
            $process.Refresh()
        }
        $output = @(
            if (Test-Path $stdoutPath) { Get-Content $stdoutPath }
            if (Test-Path $stderrPath) { Get-Content $stderrPath }
        ) -join [Environment]::NewLine
        if ($output) { Write-Host $output }
        if ($output -match "not enabled") {
            throw "Tailscale needs one-time tailnet approval. Open the authorization link printed above, approve it, then run StartInternetAccess.cmd again."
        }
        if (-not $finished) {
            throw "Tailscale did not finish configuring within 15 seconds."
        }
        if ($null -ne $process.ExitCode -and $process.ExitCode -ne 0) {
            throw "Tailscale exited with code $($process.ExitCode)."
        }
    } finally {
        Remove-Item $stdoutPath, $stderrPath -ErrorAction SilentlyContinue
    }
}

function Get-TailscaleStatus {
    try {
        return (& $tailscale status --json 2>$null | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Wait-TailscaleReady {
    $status = Get-TailscaleStatus
    if ($status -and $status.BackendState -eq "Running" -and $status.Self.Online -and $status.Self.DNSName) {
        return $status
    }

    Write-Host "Refreshing stale Tailscale network state..." -ForegroundColor Yellow
    & $tailscale debug clear-netmap-cache 2>$null | Out-Null
    & $tailscale debug force-netmap-update 2>$null | Out-Null
    & $tailscale up 2>$null | Out-Null

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $status = Get-TailscaleStatus
        if ($status -and $status.BackendState -eq "Running" -and $status.Self.Online -and $status.Self.DNSName) {
            return $status
        }
    }

    $state = if ($status) { [string]$status.BackendState } else { "unavailable" }
    throw "Tailscale did not become ready after refreshing its network map. Backend state: $state. Open Tailscale once to confirm this computer is signed in."
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$AdminApiPort/health" -TimeoutSec 5
    if (-not $health.ok) { throw "Server health check returned ok=false" }
} catch {
    throw "Bat Node server is not responding on port $AdminApiPort. Start DashboardApp.bat first. $($_.Exception.Message)"
}

try {
    $gatewayHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$DeviceGatewayPort/health" -TimeoutSec 5
    if (-not $gatewayHealth.ok) { throw "Device gateway health check returned ok=false" }
} catch {
    throw "ESP32 device gateway is not responding on port $DeviceGatewayPort. Start DashboardApp.bat first. $($_.Exception.Message)"
}

try {
    $dashboard = Invoke-WebRequest -Uri "http://127.0.0.1:$DashboardPort" -UseBasicParsing -TimeoutSec 5
    if ($dashboard.StatusCode -ne 200) { throw "Dashboard returned HTTP $($dashboard.StatusCode)" }
} catch {
    throw "Bat Node dashboard is not responding on port $DashboardPort. Start DashboardApp.bat first. $($_.Exception.Message)"
}

Write-Host "Publishing the ESP32 API through Tailscale Funnel..." -ForegroundColor Cyan
$status = Wait-TailscaleReady
Invoke-TailscaleConfig @("funnel", "--bg", "--yes", "--https=443", "http://127.0.0.1:$DeviceGatewayPort")

Write-Host "Publishing the dashboard privately inside your tailnet..." -ForegroundColor Cyan
Invoke-TailscaleConfig @("serve", "--bg", "--yes", "--https=8443", "http://127.0.0.1:$DashboardPort")

$dnsName = [string]$status.Self.DNSName
$dnsName = $dnsName.TrimEnd('.')

Write-Host ""
Write-Host "Bat Node internet access is ready" -ForegroundColor Green
Write-Host "================================="
Write-Host "ESP32 server URL: https://$dnsName"
Write-Host "Private dashboard: https://${dnsName}:8443"
Write-Host ""
Write-Host "Use the ESP32 server URL in the node setup page."
Write-Host "The dashboard URL works from devices signed into your Tailscale network."
Write-Host ""
& $tailscale funnel status
& $tailscale serve status
