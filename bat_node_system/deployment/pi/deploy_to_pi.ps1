param(
    [Parameter(Mandatory = $true)]
    [string]$PiHost,

    [string]$PiUser = "pchem",
    [string]$PiHostname = "raspberrypi",
    [string]$RemoteDir = "",
    [switch]$SkipRuntimeData,
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH."
    }
}

Require-Command ssh
Require-Command scp
Require-Command tar
Require-Command robocopy

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RemoteDir = if ($RemoteDir) { $RemoteDir } else { "/home/$PiUser/bat_node_system" }

$DeployRoot = Join-Path ([System.IO.Path]::GetTempPath()) "bat_node_system_pi_deploy"
$StagingDir = Join-Path $DeployRoot "bat_node_system"
$ArchivePath = Join-Path $DeployRoot "bat_node_system.tar.gz"

$DeployRootResolved = [System.IO.Path]::GetFullPath($DeployRoot)
$TempResolved = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
if (-not $DeployRootResolved.StartsWith($TempResolved, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove staging path outside temp: $DeployRootResolved"
}

if (Test-Path $DeployRootResolved) {
    Remove-Item -LiteralPath $DeployRootResolved -Recurse -Force
}
New-Item -ItemType Directory -Path $StagingDir | Out-Null

$excludeDirs = @(".venv", "__pycache__", ".pytest_cache")
$excludeFiles = @("*.pyc", "*.pyo", "*.part", "bat_dashboard_app.py.before_width_patch")
if ($SkipRuntimeData) {
    $excludeDirs += @("data")
    $excludeFiles += @("bat_nodes_v2.db")
}

Write-Host "Staging project from $ProjectRoot"
$robocopyArgs = @(
    $ProjectRoot,
    $StagingDir,
    "/MIR",
    "/XD"
) + $excludeDirs + @("/XF") + $excludeFiles + @("/NFL", "/NDL", "/NJH", "/NJS", "/NP")

& robocopy @robocopyArgs
if ($LASTEXITCODE -gt 7) {
    throw "robocopy failed with exit code $LASTEXITCODE"
}

Write-Host "Creating archive $ArchivePath"
if (Test-Path $ArchivePath) {
    Remove-Item -LiteralPath $ArchivePath -Force
}
& tar -czf $ArchivePath -C $StagingDir .

$Target = "$PiUser@$PiHost"
$SshOptions = @("-o", "StrictHostKeyChecking=accept-new")
Write-Host "Creating remote directory $RemoteDir on $Target"
& ssh @SshOptions $Target "mkdir -p '$RemoteDir'"

Write-Host "Copying archive to $Target"
& scp @SshOptions $ArchivePath "${Target}:/tmp/bat_node_system.tar.gz"

Write-Host "Extracting project on Pi"
& ssh @SshOptions $Target "tar -xzf /tmp/bat_node_system.tar.gz -C '$RemoteDir' && chmod +x '$RemoteDir/deployment/pi/install_pi.sh'"

if (-not $NoInstall) {
    Write-Host "Running Pi installer"
    & ssh @SshOptions "-tt" $Target "APP_DIR='$RemoteDir' PI_HOSTNAME='$PiHostname' bash '$RemoteDir/deployment/pi/install_pi.sh'"
}

Write-Host ""
Write-Host "Deployment complete."
Write-Host "Server check:    http://${PiHost}:8000/v1/public/server_time"
Write-Host "Dashboard check: http://${PiHost}:8501"
