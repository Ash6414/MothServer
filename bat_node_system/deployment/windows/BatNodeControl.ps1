$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class BatNodeWindowTheme {
    [DllImport("dwmapi.dll")]
    public static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int valueSize);
}
"@
[System.Windows.Forms.Application]::EnableVisualStyles()
[System.Windows.Forms.Application]::SetUnhandledExceptionMode(
    [System.Windows.Forms.UnhandledExceptionMode]::CatchException
)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$managerScript = Join-Path $PSScriptRoot "Manage-BatNodeStack.ps1"
$logsDir = Join-Path $root "logs"
$controlLog = Join-Path $logsDir "control.log"
$stackState = Join-Path $logsDir "stack-state.json"
$tailscale = "C:\Program Files\Tailscale\tailscale.exe"
$launcher = Join-Path $root "BatNode Control.vbs"
$startupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Bat Node Control.lnk"

New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

function Write-UiLog {
    param([string]$Message, [string]$Level = "INFO")
    try {
        $line = "{0} [{1}] UI: {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
        Add-Content -LiteralPath $controlLog -Value $line
    } catch {
    }
}

function Color([string]$Hex) {
    return [System.Drawing.ColorTranslator]::FromHtml($Hex)
}

$colors = @{
    Background = Color "#0B0F14"
    Surface = Color "#111821"
    SurfaceAlt = Color "#151E28"
    Border = Color "#253240"
    Text = Color "#E8EEF4"
    Muted = Color "#8B9AAA"
    Cyan = Color "#2FB7D3"
    Green = Color "#3CCB7F"
    Amber = Color "#E5AA3D"
    Red = Color "#E35D6A"
}

$createdNew = $false
$uiMutex = New-Object System.Threading.Mutex($true, "Local\BatNodeControlApp", [ref]$createdNew)
if (-not $createdNew) {
    [System.Windows.Forms.MessageBox]::Show(
        "Bat Node Control is already open.",
        "Bat Node Control",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    ) | Out-Null
    exit 0
}

$fontUi = [System.Drawing.Font]::new("Segoe UI", 10)
$fontSmall = [System.Drawing.Font]::new("Segoe UI", 9)
$fontTitle = [System.Drawing.Font]::new("Segoe UI Semibold", 17)
$fontSection = [System.Drawing.Font]::new("Segoe UI Semibold", 10)
$fontMono = [System.Drawing.Font]::new("Cascadia Mono", 9)

$form = New-Object System.Windows.Forms.Form
$form.Text = "Bat Node Control"
$form.Size = [System.Drawing.Size]::new(940, 720)
$form.MinimumSize = [System.Drawing.Size]::new(820, 660)
$form.StartPosition = "CenterScreen"
$form.BackColor = $colors.Background
$form.ForeColor = $colors.Text
$form.Font = $fontUi
$form.Icon = [System.Drawing.SystemIcons]::Application

$title = New-Object System.Windows.Forms.Label
$title.Text = "BAT NODE CONTROL"
$title.Font = $fontTitle
$title.ForeColor = $colors.Text
$title.AutoSize = $true
$title.Location = [System.Drawing.Point]::new(28, 20)
$form.Controls.Add($title)

$subtitle = New-Object System.Windows.Forms.Label
$subtitle.Text = "AudioMoth ingest and remote access"
$subtitle.Font = $fontSmall
$subtitle.ForeColor = $colors.Muted
$subtitle.AutoSize = $true
$subtitle.Location = [System.Drawing.Point]::new(30, 55)
$form.Controls.Add($subtitle)

$overallDot = New-Object System.Windows.Forms.Panel
$overallDot.Size = [System.Drawing.Size]::new(12, 12)
$overallDot.Location = [System.Drawing.Point]::new(732, 29)
$overallDot.Tag = $colors.Amber
$overallDot.Anchor = "Top,Right"
$overallDot.Add_Paint({
    param($sender, $eventArgs)
    $brush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]$sender.Tag)
    $eventArgs.Graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $eventArgs.Graphics.FillEllipse($brush, 1, 1, 10, 10)
    $brush.Dispose()
})
$form.Controls.Add($overallDot)

$overallLabel = New-Object System.Windows.Forms.Label
$overallLabel.Text = "STARTING"
$overallLabel.Font = $fontSection
$overallLabel.ForeColor = $colors.Amber
$overallLabel.TextAlign = "MiddleRight"
$overallLabel.Size = [System.Drawing.Size]::new(145, 24)
$overallLabel.Location = [System.Drawing.Point]::new(750, 22)
$overallLabel.Anchor = "Top,Right"
$form.Controls.Add($overallLabel)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Style = "Marquee"
$progress.MarqueeAnimationSpeed = 24
$progress.Location = [System.Drawing.Point]::new(28, 82)
$progress.Size = [System.Drawing.Size]::new(866, 3)
$progress.Anchor = "Top,Left,Right"
$form.Controls.Add($progress)

$servicesHeading = New-Object System.Windows.Forms.Label
$servicesHeading.Text = "SERVICES"
$servicesHeading.Font = $fontSection
$servicesHeading.ForeColor = $colors.Muted
$servicesHeading.AutoSize = $true
$servicesHeading.Location = [System.Drawing.Point]::new(28, 101)
$form.Controls.Add($servicesHeading)

$script:statusRows = @{}

function Add-ServiceRow {
    param([string]$Name, [string]$Detail, [int]$Y)
    $row = New-Object System.Windows.Forms.Panel
    $row.Location = [System.Drawing.Point]::new(28, $Y)
    $row.Size = [System.Drawing.Size]::new(866, 42)
    $row.Anchor = "Top,Left,Right"
    $row.BackColor = $colors.Surface
    $form.Controls.Add($row)

    $dot = New-Object System.Windows.Forms.Panel
    $dot.Size = [System.Drawing.Size]::new(12, 12)
    $dot.Location = [System.Drawing.Point]::new(16, 15)
    $dot.Tag = $colors.Muted
    $dot.Add_Paint({
        param($sender, $eventArgs)
        $brush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]$sender.Tag)
        $eventArgs.Graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $eventArgs.Graphics.FillEllipse($brush, 1, 1, 10, 10)
        $brush.Dispose()
    })
    $row.Controls.Add($dot)

    $nameLabel = New-Object System.Windows.Forms.Label
    $nameLabel.Text = $Name
    $nameLabel.ForeColor = $colors.Text
    $nameLabel.Location = [System.Drawing.Point]::new(43, 10)
    $nameLabel.Size = [System.Drawing.Size]::new(180, 24)
    $row.Controls.Add($nameLabel)

    $detailLabel = New-Object System.Windows.Forms.Label
    $detailLabel.Text = $Detail
    $detailLabel.Font = $fontMono
    $detailLabel.ForeColor = $colors.Muted
    $detailLabel.Location = [System.Drawing.Point]::new(230, 10)
    $detailLabel.Size = [System.Drawing.Size]::new(440, 24)
    $detailLabel.Anchor = "Top,Left,Right"
    $row.Controls.Add($detailLabel)

    $stateLabel = New-Object System.Windows.Forms.Label
    $stateLabel.Text = "CHECKING"
    $stateLabel.Font = $fontSmall
    $stateLabel.ForeColor = $colors.Muted
    $stateLabel.TextAlign = "MiddleRight"
    $stateLabel.Location = [System.Drawing.Point]::new(706, 9)
    $stateLabel.Size = [System.Drawing.Size]::new(140, 24)
    $stateLabel.Anchor = "Top,Right"
    $row.Controls.Add($stateLabel)

    $script:statusRows[$Name] = @{ Dot = $dot; State = $stateLabel; Detail = $detailLabel }
}

Add-ServiceRow -Name "Server API" -Detail "127.0.0.1:8000" -Y 124
Add-ServiceRow -Name "Device Gateway" -Detail "127.0.0.1:8001" -Y 168
Add-ServiceRow -Name "Dashboard" -Detail "127.0.0.1:8501" -Y 212
Add-ServiceRow -Name "Tailscale" -Detail "Checking tailnet connection" -Y 256

$accessHeading = New-Object System.Windows.Forms.Label
$accessHeading.Text = "REMOTE ACCESS"
$accessHeading.Font = $fontSection
$accessHeading.ForeColor = $colors.Muted
$accessHeading.AutoSize = $true
$accessHeading.Location = [System.Drawing.Point]::new(28, 320)
$form.Controls.Add($accessHeading)

$accessPanel = New-Object System.Windows.Forms.Panel
$accessPanel.Location = [System.Drawing.Point]::new(28, 343)
$accessPanel.Size = [System.Drawing.Size]::new(866, 86)
$accessPanel.Anchor = "Top,Left,Right"
$accessPanel.BackColor = $colors.Surface
$form.Controls.Add($accessPanel)

$publicCaption = New-Object System.Windows.Forms.Label
$publicCaption.Text = "DEVICE API"
$publicCaption.Font = $fontSmall
$publicCaption.ForeColor = $colors.Muted
$publicCaption.Location = [System.Drawing.Point]::new(16, 11)
$publicCaption.Size = [System.Drawing.Size]::new(110, 20)
$accessPanel.Controls.Add($publicCaption)

$publicUrl = New-Object System.Windows.Forms.Label
$publicUrl.Text = "Waiting for Tailscale"
$publicUrl.Font = $fontMono
$publicUrl.ForeColor = $colors.Text
$publicUrl.Location = [System.Drawing.Point]::new(135, 10)
$publicUrl.Size = [System.Drawing.Size]::new(700, 22)
$publicUrl.Anchor = "Top,Left,Right"
$accessPanel.Controls.Add($publicUrl)

$privateCaption = New-Object System.Windows.Forms.Label
$privateCaption.Text = "DASHBOARD"
$privateCaption.Font = $fontSmall
$privateCaption.ForeColor = $colors.Muted
$privateCaption.Location = [System.Drawing.Point]::new(16, 48)
$privateCaption.Size = [System.Drawing.Size]::new(110, 20)
$accessPanel.Controls.Add($privateCaption)

$privateUrl = New-Object System.Windows.Forms.Label
$privateUrl.Text = "http://127.0.0.1:8501"
$privateUrl.Font = $fontMono
$privateUrl.ForeColor = $colors.Text
$privateUrl.Location = [System.Drawing.Point]::new(135, 47)
$privateUrl.Size = [System.Drawing.Size]::new(700, 22)
$privateUrl.Anchor = "Top,Left,Right"
$accessPanel.Controls.Add($privateUrl)

function New-CommandButton {
    param([string]$Text, [int]$X, [int]$Width = 142)
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text
    $button.FlatStyle = "Flat"
    $button.FlatAppearance.BorderColor = $colors.Border
    $button.FlatAppearance.MouseOverBackColor = $colors.SurfaceAlt
    $button.FlatAppearance.MouseDownBackColor = $colors.Border
    $button.BackColor = $colors.Surface
    $button.ForeColor = $colors.Text
    $button.Size = [System.Drawing.Size]::new($Width, 38)
    $button.Location = [System.Drawing.Point]::new($X, 447)
    return $button
}

$openButton = New-CommandButton -Text "Open Dashboard" -X 28 -Width 154
$startButton = New-CommandButton -Text "Start" -X 190 -Width 112
$restartButton = New-CommandButton -Text "Restart" -X 310 -Width 112
$stopButton = New-CommandButton -Text "Stop" -X 430 -Width 112
$logsButton = New-CommandButton -Text "Open Logs" -X 550 -Width 126
$form.Controls.AddRange(@($openButton, $startButton, $restartButton, $stopButton, $logsButton))

$startupCheck = New-Object System.Windows.Forms.CheckBox
$startupCheck.Text = "Launch at sign-in"
$startupCheck.ForeColor = $colors.Muted
$startupCheck.AutoSize = $true
$startupCheck.Location = [System.Drawing.Point]::new(750, 456)
$startupCheck.Anchor = "Top,Right"
$startupCheck.Checked = Test-Path -LiteralPath $startupShortcut
$form.Controls.Add($startupCheck)

$activityHeading = New-Object System.Windows.Forms.Label
$activityHeading.Text = "ACTIVITY"
$activityHeading.Font = $fontSection
$activityHeading.ForeColor = $colors.Muted
$activityHeading.AutoSize = $true
$activityHeading.Location = [System.Drawing.Point]::new(28, 503)
$form.Controls.Add($activityHeading)

$activity = New-Object System.Windows.Forms.RichTextBox
$activity.Location = [System.Drawing.Point]::new(28, 526)
$activity.Size = [System.Drawing.Size]::new(866, 118)
$activity.Anchor = "Top,Bottom,Left,Right"
$activity.BackColor = Color "#070A0E"
$activity.ForeColor = Color "#B7C5D2"
$activity.BorderStyle = "FixedSingle"
$activity.Font = $fontMono
$activity.ReadOnly = $true
$activity.DetectUrls = $false
$activity.WordWrap = $false
$form.Controls.Add($activity)

$footer = New-Object System.Windows.Forms.Label
$footer.Text = "Closing this window keeps the services running."
$footer.Font = $fontSmall
$footer.ForeColor = $colors.Muted
$footer.AutoSize = $true
$footer.Location = [System.Drawing.Point]::new(28, 654)
$footer.Anchor = "Bottom,Left"
$form.Controls.Add($footer)

$script:managerProcess = $null
$script:dnsName = ""
$script:tailnetTick = 0
$script:lastLog = ""

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(300)) { return $false }
        $client.EndConnect($result)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Set-ServiceState {
    param([string]$Name, [bool]$Online, [bool]$Starting = $false)
    $entry = $script:statusRows[$Name]
    if ($Online) {
        $entry.Dot.Tag = $colors.Green
        $entry.State.Text = "ONLINE"
        $entry.State.ForeColor = $colors.Green
    } elseif ($Starting) {
        $entry.Dot.Tag = $colors.Amber
        $entry.State.Text = "STARTING"
        $entry.State.ForeColor = $colors.Amber
    } else {
        $entry.Dot.Tag = $colors.Red
        $entry.State.Text = "OFFLINE"
        $entry.State.ForeColor = $colors.Red
    }
    $entry.Dot.Invalidate()
}

function Start-StackAction([string]$Action) {
    if ($script:managerProcess -and -not $script:managerProcess.HasExited) {
        return
    }
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$managerScript`"", "-Action", $Action
    )
    $script:managerProcess = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WindowStyle Hidden -PassThru
    $progress.Style = "Marquee"
    $progress.Visible = $true
}

function Refresh-Tailscale {
    try {
        $liveStatus = $null
        if (Test-Path -LiteralPath $tailscale) {
            $liveStatus = (& $tailscale status --json 2>$null | ConvertFrom-Json)
        }
        if ($liveStatus -and $liveStatus.BackendState -eq "Running" -and $liveStatus.Self.Online) {
            $script:dnsName = ([string]$liveStatus.Self.DNSName).TrimEnd('.')
        } elseif (Test-Path -LiteralPath $stackState) {
            $cachedStatus = Get-Content -LiteralPath $stackState -Raw | ConvertFrom-Json
            $script:dnsName = if ($cachedStatus.online) { ([string]$cachedStatus.dns_name).TrimEnd('.') } else { "" }
        } else {
            $script:dnsName = ""
        }
        if ($script:dnsName) {
            $script:statusRows["Tailscale"].Detail.Text = $script:dnsName
            $publicUrl.Text = "https://$($script:dnsName)"
            $privateUrl.Text = "https://$($script:dnsName):8443"
            return $true
        }
    } catch {
    }
    $script:dnsName = ""
    $script:statusRows["Tailscale"].Detail.Text = "Tailnet connection unavailable"
    $publicUrl.Text = "Waiting for Tailscale"
    $privateUrl.Text = "http://127.0.0.1:8501"
    return $false
}

function Refresh-Activity {
    if (-not (Test-Path -LiteralPath $controlLog)) { return }
    try {
        $content = (Get-Content -LiteralPath $controlLog -Tail 80 -ErrorAction Stop) -join [Environment]::NewLine
        if ($content -ne $script:lastLog) {
            $script:lastLog = $content
            $activity.Text = $content
            $activity.SelectionStart = $activity.TextLength
            $activity.ScrollToCaret()
        }
    } catch {
    }
}

function Refresh-Status {
    $managerRunning = $script:managerProcess -and -not $script:managerProcess.HasExited
    $serverOnline = Test-Port 8000
    $gatewayOnline = Test-Port 8001
    $dashboardOnline = Test-Port 8501

    $script:tailnetTick++
    if ($script:tailnetTick -eq 1 -or $script:tailnetTick -ge 8) {
        $script:tailnetTick = 1
        $tailscaleOnline = Refresh-Tailscale
    } else {
        $tailscaleOnline = -not [string]::IsNullOrWhiteSpace($script:dnsName)
    }

    Set-ServiceState "Server API" $serverOnline ($managerRunning -and -not $serverOnline)
    Set-ServiceState "Device Gateway" $gatewayOnline ($managerRunning -and -not $gatewayOnline)
    Set-ServiceState "Dashboard" $dashboardOnline ($managerRunning -and -not $dashboardOnline)
    Set-ServiceState "Tailscale" $tailscaleOnline ($managerRunning -and -not $tailscaleOnline)

    $allOnline = $serverOnline -and $gatewayOnline -and $dashboardOnline -and $tailscaleOnline
    if ($allOnline) {
        $overallDot.Tag = $colors.Green
        $overallLabel.Text = "SYSTEM ONLINE"
        $overallLabel.ForeColor = $colors.Green
        $progress.Visible = $false
    } elseif ($managerRunning) {
        $overallDot.Tag = $colors.Amber
        $overallLabel.Text = "STARTING"
        $overallLabel.ForeColor = $colors.Amber
        $progress.Visible = $true
    } else {
        $overallDot.Tag = $colors.Red
        $overallLabel.Text = "ACTION NEEDED"
        $overallLabel.ForeColor = $colors.Red
        $progress.Visible = $false
    }
    $overallDot.Invalidate()
    $openButton.Enabled = $dashboardOnline
    $startButton.Enabled = -not $managerRunning
    $restartButton.Enabled = -not $managerRunning
    $stopButton.Enabled = -not $managerRunning
    Refresh-Activity
}

$openButton.Add_Click({
    $uri = if ($script:dnsName) { "https://$($script:dnsName):8443" } else { "http://127.0.0.1:8501" }
    Start-Process $uri
})
$startButton.Add_Click({ Start-StackAction "Start" })
$restartButton.Add_Click({ Start-StackAction "Restart" })
$stopButton.Add_Click({ Start-StackAction "Stop" })
$logsButton.Add_Click({ Start-Process "explorer.exe" -ArgumentList "`"$logsDir`"" })

$startupCheck.Add_CheckedChanged({
    try {
        if ($startupCheck.Checked) {
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($startupShortcut)
            $shortcut.TargetPath = "wscript.exe"
            $shortcut.Arguments = "`"$launcher`""
            $shortcut.WorkingDirectory = $root
            $shortcut.Description = "Bat Node Control"
            $shortcut.Save()
        } else {
            Remove-Item -LiteralPath $startupShortcut -Force -ErrorAction SilentlyContinue
        }
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "Startup setting") | Out-Null
    }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1500
$timer.Add_Tick({
    try {
        Refresh-Status
    } catch {
        Write-UiLog $_.Exception.Message "WARN"
        $overallDot.Tag = $colors.Amber
        $overallLabel.Text = "REFRESH WARNING"
        $overallLabel.ForeColor = $colors.Amber
        $overallDot.Invalidate()
    }
})

$form.Add_Shown({
    try {
        $darkTitleBar = 1
        [BatNodeWindowTheme]::DwmSetWindowAttribute($form.Handle, 20, [ref]$darkTitleBar, 4) | Out-Null
    } catch {
    }
    try {
        Write-UiLog "Control app opened."
        Refresh-Status
        Start-StackAction "Start"
        $timer.Start()
    } catch {
        Write-UiLog $_.Exception.Message "ERROR"
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "Bat Node Control") | Out-Null
    }
})

$form.Add_FormClosed({
    $timer.Stop()
    Write-UiLog "Control app closed."
    try { $uiMutex.ReleaseMutex() } catch {}
    try { $uiMutex.Dispose() } catch {}
})

$threadExceptionHandler = [System.Threading.ThreadExceptionEventHandler]{
    param($sender, $eventArgs)
    Write-UiLog $eventArgs.Exception.ToString() "ERROR"
    $overallDot.Tag = $colors.Red
    $overallLabel.Text = "UI ERROR"
    $overallLabel.ForeColor = $colors.Red
    $overallDot.Invalidate()
}
[System.Windows.Forms.Application]::add_ThreadException($threadExceptionHandler)

try {
    [System.Windows.Forms.Application]::Run($form)
    Write-UiLog "Window message loop ended."
} catch {
    Write-UiLog $_.Exception.ToString() "ERROR"
} finally {
    try { [System.Windows.Forms.Application]::remove_ThreadException($threadExceptionHandler) } catch {}
}
