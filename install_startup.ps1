# Adds (or removes) a Windows Startup shortcut so Wi-Fi Sense starts when you log in.
#
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1                # tray icon, no console (pythonw tray.py)
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Mode Exe      # packaged dist\WiFiSense\WiFiSense.exe
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Mode Console  # console window (start_wifi_sense.bat)
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Remove
#
# XAMPP's MySQL must also start at login
# (XAMPP Control Panel > Config > Autostart of modules: MySQL).

param([ValidateSet("Tray", "Exe", "Console")] [string]$Mode = "Tray", [switch]$Remove)

$startup  = [Environment]::GetFolderPath("Startup")
$shortcut = Join-Path $startup "Wi-Fi Sense.lnk"

if ($Remove) {
    if (Test-Path $shortcut) { Remove-Item $shortcut; "Removed $shortcut" } else { "Not installed." }
    return
}

$sh = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
switch ($Mode) {
    "Tray" {
        $pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
        if (-not $pyw) { throw "pythonw not found on PATH; use -Mode Console or -Mode Exe" }
        $sh.TargetPath = $pyw
        $sh.Arguments  = "`"$(Join-Path $PSScriptRoot 'tray.py')`""
        $sh.WorkingDirectory = $PSScriptRoot
    }
    "Exe" {
        $exe = Join-Path $PSScriptRoot "dist\WiFiSense\WiFiSense.exe"
        if (-not (Test-Path $exe)) { throw "Build it first: build_exe.ps1" }
        $sh.TargetPath = $exe
        $sh.WorkingDirectory = Split-Path $exe
    }
    "Console" {
        $sh.TargetPath = Join-Path $PSScriptRoot "start_wifi_sense.bat"
        $sh.WorkingDirectory = $PSScriptRoot
        $sh.WindowStyle = 7          # minimised
    }
}
$sh.Description = "Wi-Fi Sense passive motion dashboard"
$sh.Save()
"Installed ($Mode): $shortcut"

$envDir = if ($Mode -eq "Exe") { Join-Path $PSScriptRoot "dist\WiFiSense" } else { $PSScriptRoot }
if (-not (Test-Path (Join-Path $envDir ".env"))) {
    "Note: create .env in $envDir (copy .env.example) with WIFI_SENSE_DB_PASSWORD so it can log in."
}
