# Removes what is left of the old "pzmodcheck" name.
#
# The tool was renamed to pzmodmanager. The old package folder, its launcher, its
# build recipe, its test file and its old build output are all still sitting in
# this folder doing nothing except confusing you and PyInstaller.
#
# WARNING, read this before running it.
#
#   This DELETES files. It touches nothing outside this folder, nothing named
#   pzmodmanager, and none of your mods, saves or server files. But a delete is a
#   delete, so it shows you the exact list first and does nothing until you type
#   YES.
#
#   If you would rather keep a way back, copy the whole folder somewhere first:
#       Copy-Item -Recurse "C:\Users\leo\Documents\pz\pzmodcheck" "$env:USERPROFILE\Desktop\pz-backup"
#
# Run it from this folder:
#       powershell -ExecutionPolicy Bypass -File .\cleanup-old-name.ps1

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$targets = @(
    "pzmodcheck",                      # the old package folder
    "run-pzmodcheck.py",               # the old launcher
    "pzmodcheck.spec",                 # the old PyInstaller recipe
    "tests\test_pzmodcheck.py",        # the old test file
    "build",                           # PyInstaller scratch, rebuilt every time
    "dist\pzmodcheck.exe",             # the executable built under the old name
    "dist\pzmodcheck-report.html",
    "pzmodcheck-report.html",          # reports written under the old name
    "pzmodcheck-modlist.txt",
    "pzmodcheck-server.ini.txt",
    "pzmodcheck-workshop-links.txt"
)

$found = @()
foreach ($t in $targets) {
    if (Test-Path -LiteralPath $t) { $found += $t }
}

if ($found.Count -eq 0) {
    Write-Host "Nothing left over. This folder is already clean." -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "These will be DELETED from $here" -ForegroundColor Yellow
Write-Host ""
foreach ($t in $found) {
    $kind = if (Test-Path -LiteralPath $t -PathType Container) { "folder" } else { "file  " }
    Write-Host "  $kind  $t"
}
Write-Host ""
Write-Host "Nothing named pzmodmanager is touched, and nothing outside this folder."
Write-Host ""

$answer = Read-Host "Type YES to delete them, anything else to cancel"
if ($answer -ne "YES") {
    Write-Host "Cancelled, nothing was deleted." -ForegroundColor Green
    exit 0
}

foreach ($t in $found) {
    try {
        Remove-Item -LiteralPath $t -Recurse -Force
        Write-Host "  removed  $t"
    } catch {
        Write-Host "  FAILED   $t : $_" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Done. Rebuild with:  pyinstaller pzmodmanager.spec" -ForegroundColor Green
