$ErrorActionPreference = "Stop"
$tailscale = "C:\Program Files\Tailscale\tailscale.exe"

if (-not (Test-Path -LiteralPath $tailscale)) {
    throw "Tailscale is not installed at $tailscale"
}

& $tailscale funnel reset
if ($LASTEXITCODE -ne 0) { throw "Could not reset Tailscale Funnel" }
& $tailscale serve reset
if ($LASTEXITCODE -ne 0) { throw "Could not reset Tailscale Serve" }

Write-Host "Bat Node Funnel and Serve configuration removed." -ForegroundColor Green
