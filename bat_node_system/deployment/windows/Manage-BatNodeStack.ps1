param(
    [ValidateSet("Start", "Restart", "Stop")]
    [string]$Action = "Start"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$serverDir = Join-Path $root "server"
$dashboardDir = Join-Path $root "dashboard"
$logsDir = Join-Path $root "logs"
$serverPython = Join-Path $serverDir ".venv\Scripts\python.exe"
$dashboardPython = Join-Path $dashboardDir ".venv\Scripts\python.exe"
$controlLog = Join-Path $logsDir "control.log"
$stackState = Join-Path $logsDir "stack-state.json"
$internetStart = Join-Path $PSScriptRoot "Start-BatNodeInternet.ps1"
$internetStop = Join-Path $PSScriptRoot "Stop-BatNodeInternet.ps1"
$tailscale = "C:\Program Files\Tailscale\tailscale.exe"

New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

function Write-ControlLog {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Add-Content -LiteralPath $controlLog -Value $line
}

function Write-StackState {
    param([bool]$Online, [string]$DnsName = "")
    @{
        online = $Online
        dns_name = $DnsName
        updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    } | ConvertTo-Json | Set-Content -LiteralPath $stackState -Encoding UTF8
}

function Get-ListeningProcess {
    param([int]$Port)
    return Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Test-HttpEndpoint {
    param([string]$Uri)
    try {
        $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-HttpEndpoint {
    param([string]$Name, [string]$Uri, [int]$TimeoutSeconds = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpEndpoint $Uri) {
            Write-ControlLog "$Name is ready."
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name did not become ready within $TimeoutSeconds seconds."
}

function Start-ManagedService {
    param(
        [string]$Name,
        [int]$Port,
        [string]$Python,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [string]$HealthUri,
        [string]$LogPrefix
    )

    if (Test-HttpEndpoint $HealthUri) {
        Write-ControlLog "$Name is already running on port $Port."
        return
    }

    $listener = Get-ListeningProcess $Port
    if ($listener) {
        throw "Port $Port is occupied by process $($listener.OwningProcess), but $Name is not healthy."
    }
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "$Name runtime is missing: $Python"
    }

    Write-ControlLog "Starting $Name on port $Port."
    Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logsDir "$LogPrefix.out.log") `
        -RedirectStandardError (Join-Path $logsDir "$LogPrefix.err.log") | Out-Null
    Wait-HttpEndpoint -Name $Name -Uri $HealthUri
}

function Stop-ManagedPort {
    param([string]$Name, [int]$Port)
    $listener = Get-ListeningProcess $Port
    if (-not $listener) {
        Write-ControlLog "$Name is already stopped."
        return
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if (-not $process -or -not $process.CommandLine -or $process.CommandLine -notlike "*$root*") {
        throw "Refusing to stop process $($listener.OwningProcess) on port $Port because it is not part of this Bat Node project."
    }
    Stop-Process -Id $listener.OwningProcess -Force
    Write-ControlLog "Stopped $Name."
}

function Stop-Stack {
    Write-ControlLog "Stopping Bat Node stack."
    Write-StackState -Online $false
    if (Test-Path -LiteralPath $internetStop) {
        try {
            $output = & $internetStop 2>&1
            foreach ($line in $output) { Write-ControlLog ([string]$line) }
        } catch {
            Write-ControlLog "Tailscale publishing stop warning: $($_.Exception.Message)" "WARN"
        }
    }
    Stop-ManagedPort -Name "Dashboard" -Port 8501
    Stop-ManagedPort -Name "Device Gateway" -Port 8001
    Stop-ManagedPort -Name "Server API" -Port 8000
    Write-ControlLog "Bat Node stack stopped."
}

$mutex = New-Object System.Threading.Mutex($false, "Local\BatNodeStackManager")
$hasMutex = $false
try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) {
        Write-ControlLog "Another stack action is already running." "WARN"
        exit 2
    }

    if ($Action -in @("Restart", "Stop")) {
        Stop-Stack
        if ($Action -eq "Stop") { exit 0 }
        Start-Sleep -Seconds 1
    }

    Write-ControlLog "Starting Bat Node stack."
    $env:BAT_DB_PATH = Join-Path $serverDir "bat_nodes_v2.db"
    $env:BAT_DATA_DIR = Join-Path $serverDir "data"

    Start-ManagedService -Name "Server API" -Port 8000 -Python $serverPython `
        -Arguments @("-m", "uvicorn", "bat_server_runtime:app", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $serverDir -HealthUri "http://127.0.0.1:8000/health" -LogPrefix "server"

    Start-ManagedService -Name "Device Gateway" -Port 8001 -Python $serverPython `
        -Arguments @("-m", "uvicorn", "bat_public_gateway:app", "--host", "0.0.0.0", "--port", "8001") `
        -WorkingDirectory $serverDir -HealthUri "http://127.0.0.1:8001/health" -LogPrefix "gateway"

    Start-ManagedService -Name "Dashboard" -Port 8501 -Python $dashboardPython `
        -Arguments @("-m", "streamlit", "run", "bat_dashboard_app.py", "--server.address", "127.0.0.1", "--server.port", "8501", "--server.headless", "true") `
        -WorkingDirectory $dashboardDir -HealthUri "http://127.0.0.1:8501" -LogPrefix "dashboard"

    Write-ControlLog "Configuring Tailscale Funnel and private dashboard access."
    $tailscaleOutput = & $internetStart 2>&1
    foreach ($line in $tailscaleOutput) { Write-ControlLog ([string]$line) }
    $dnsName = ""
    if (Test-Path -LiteralPath $tailscale) {
        try {
            $tailscaleStatus = (& $tailscale status --json 2>$null | ConvertFrom-Json)
            $dnsName = ([string]$tailscaleStatus.Self.DNSName).TrimEnd('.')
        } catch {
            Write-ControlLog "Could not read the Tailscale DNS name after startup." "WARN"
        }
    }
    Write-StackState -Online (-not [string]::IsNullOrWhiteSpace($dnsName)) -DnsName $dnsName
    Write-ControlLog "Bat Node stack is ready."
    exit 0
} catch {
    Write-ControlLog $_.Exception.Message "ERROR"
    exit 1
} finally {
    if ($hasMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
