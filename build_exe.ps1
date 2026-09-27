# Builds standalone Windows programs with PyInstaller (no Python needed on the target PC):
#   dist\WiFiSense\WiFiSense.exe          tray app: dashboard + sensor in the background
#   dist\WiFiSenseNode\WiFiSenseNode.exe  remote room sensor (console), see README > Rooms
#
#   python -m pip install pyinstaller pystray
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# MySQL (XAMPP) is still needed on the hub PC. Put your .env next to WiFiSense.exe.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw "Tests failed; not building." }

$data = @("wifi_web.html", "history.html", "devices.html", "health.html", "floorplan.html", "rules.html",
          "tuning.html", "manifest.json", "sw.js", "icon.svg", "oui_manuf.txt", ".env.example") |
        Where-Object { Test-Path $_ } | ForEach-Object { "--add-data=$_;." }
if (Test-Path "brand") { $data += "--add-data=brand;brand" }

$common = @("--noconfirm", "--clean", "--onedir", "--collect-all", "bleak", "--collect-all", "winrt",
            "--collect-submodules", "plyer",
            "--hidden-import", "pystray._win32", "--exclude-module", "matplotlib", "--exclude-module", "numpy")

$icon = if (Test-Path "brand\favicon.ico") { "brand\favicon.ico" } else { "NONE" }
python -m PyInstaller @common @data --windowed --name WiFiSense --icon $icon tray.py
if ($LASTEXITCODE -ne 0) { throw "WiFiSense build failed" }
python -m PyInstaller @common --console --name WiFiSenseNode --icon $icon node.py
if ($LASTEXITCODE -ne 0) { throw "WiFiSenseNode build failed" }

# local config + learned state travel with the hub build (never commit .env)
foreach ($f in @(".env", "settings.json", "activity_model.json", "floorplan.json")) {
    if (Test-Path $f) { Copy-Item $f "dist\WiFiSense\$f" -Force }
}
Get-ChildItem "floorplan_image.*" -ErrorAction SilentlyContinue | Copy-Item -Destination "dist\WiFiSense\" -Force
Copy-Item ".env.example" "dist\WiFiSenseNode\.env.example" -Force

"Built:"
Get-ChildItem dist\WiFiSense\WiFiSense.exe, dist\WiFiSenseNode\WiFiSenseNode.exe | Select-Object FullName, Length
