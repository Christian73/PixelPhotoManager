#Requires -Version 5.1
# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
<#
.SYNOPSIS
    Builds the Pixel Photo Manager MSI with WiX Toolset v3.
.DESCRIPTION
    1. Checks / installs WiX Toolset v3.
    2. Generates the installer bitmaps (banner.bmp, dialog.bmp).
    3. Runs heat.exe to inventory dist\PixelPhotoManager\.
    4. Compiles with candle.exe and links with light.exe.
    5. Produces: installer\PixelPhotoManager-<version>-x64.msi

    Run from the project directory or from installer\ :
        powershell -ExecutionPolicy Bypass -File installer\build_msi.ps1
#>
$ErrorActionPreference = "Stop"

# ── Paths ────────────────────────────────────────────────────────────────────
$InstallerDir = $PSScriptRoot
$ProjectRoot  = Split-Path -Parent $InstallerDir
$DistDir      = Join-Path $ProjectRoot "dist\PixelPhotoManager"
$ObjDir       = Join-Path $InstallerDir "obj"
$VersionFile  = Join-Path $ProjectRoot "VERSION"

# ── Version number (single source: VERSION at the root of the repository) ────
# Written by build.ps1 before the EXE build; if this script is run on its own
# (outside build.ps1), it is asked for here so as not to publish an MSI with a
# stale or missing version.
if (-not (Test-Path $VersionFile)) {
    $ProductVersion = Read-Host "Numero de version du MSI (ex: 1.1.0)"
    if ($ProductVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "Numero de version invalide : '$ProductVersion' (format attendu : Major.Minor.Build, ex: 1.1.0)"
    }
    Set-Content -Path $VersionFile -Value $ProductVersion -NoNewline
} else {
    $ProductVersion = (Get-Content $VersionFile -Raw).Trim()
}
Write-Host "Version MSI : $ProductVersion"

# Versioned output name (e.g. PixelPhotoManager-1.0.1-x64.msi)
$OutputMsi = Join-Path $InstallerDir "PixelPhotoManager-$ProductVersion-x64.msi"

# ── Locate WiX v3 ────────────────────────────────────────────────────────────
function Find-Wix3 {
    if ($env:WIX) {
        $bin = Join-Path $env:WIX.TrimEnd('\') "bin"
        if (Test-Path "$bin\candle.exe") { return $bin }
    }
    foreach ($v in @("v3.14", "v3.11")) {
        $bin = "${env:ProgramFiles(x86)}\WiX Toolset $v\bin"
        if (Test-Path "$bin\candle.exe") { return $bin }
    }
    return $null
}

$WixBin = Find-Wix3
if (-not $WixBin) {
    Write-Host "WiX Toolset v3 non trouve. Installation en cours..."
    $wixInstaller = Join-Path $env:TEMP "wix314.exe"
    Write-Host "  Telechargement de wix314.exe..."
    curl.exe -L "https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314.exe" `
             -o $wixInstaller --silent --show-error
    Write-Host "  Installation (mode silencieux)..."
    Start-Process -FilePath $wixInstaller -ArgumentList "/quiet" -Wait
    Remove-Item $wixInstaller -Force
    $WixBin = Find-Wix3
    if (-not $WixBin) {
        throw "WiX v3 toujours introuvable apres installation. Redemarrez PowerShell et relancez."
    }
}

$Heat     = Join-Path $WixBin "heat.exe"
$Candle   = Join-Path $WixBin "candle.exe"
$Light    = Join-Path $WixBin "light.exe"
$WixUIExt = Join-Path $WixBin "WixUIExtension.dll"
Write-Host "WiX v3 : $WixBin"

# ── Prerequisite checks ───────────────────────────────────────────────────────
if (-not (Test-Path "$DistDir\PixelPhotoManager.exe")) {
    throw "PixelPhotoManager.exe introuvable dans $DistDir.`nLancez d'abord le build PyInstaller : .\build.ps1 -ExeOnly"
}
if (-not (Test-Path (Join-Path $ProjectRoot "assets\app_icon.ico"))) {
    throw "assets\app_icon.ico introuvable."
}

# ── Installer bitmaps ─────────────────────────────────────────────────────────
$BannerBmp = Join-Path $InstallerDir "banner.bmp"
$DialogBmp = Join-Path $InstallerDir "dialog.bmp"

if (-not (Test-Path $BannerBmp) -or -not (Test-Path $DialogBmp)) {
    Write-Host "Generation des bitmaps de l'installeur..."
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $createScript = Join-Path $InstallerDir "create_bitmaps.py"
    & $venvPython $createScript
    if ($LASTEXITCODE -ne 0) { throw "create_bitmaps.py a echoue." }
}

# ── Preparation ───────────────────────────────────────────────────────────────
if (Test-Path $ObjDir) { Remove-Item -Recurse -Force $ObjDir }
New-Item -ItemType Directory -Force -Path $ObjDir | Out-Null
Get-ChildItem -Path $InstallerDir -Filter "harvested_*.wxs" | Remove-Item -Force

# ── Harvest: application ──────────────────────────────────────────────────────
Write-Host ""
Write-Host "Inventaire de dist\PixelPhotoManager\ ..."
& $Heat dir $DistDir `
    -cg AppFiles `
    -dr INSTALLFOLDER `
    -var var.AppSourceDir `
    -srd -sreg -ag `
    -out "$InstallerDir\harvested_app.wxs" `
    -nologo
if ($LASTEXITCODE -ne 0) { throw "heat.exe a echoue pour AppFiles" }

# ── Compilation (candle.exe) ──────────────────────────────────────────────────
Write-Host ""
Write-Host "Compilation des sources WiX ..."

$wxsSources  = @(Join-Path $InstallerDir "product.wxs")
$wxsSources += Get-ChildItem -Path $InstallerDir -Filter "harvested_*.wxs" |
               Select-Object -ExpandProperty FullName

$candleDefs = @(
    "-dAppSourceDir=$DistDir",
    "-dAssetsDir=$(Join-Path $ProjectRoot 'assets')",
    "-dProductVersion=$ProductVersion"
)

& $Candle -arch x64 -out "$ObjDir\" -nologo -ext $WixUIExt @candleDefs @wxsSources
if ($LASTEXITCODE -ne 0) { throw "candle.exe a echoue" }

# ── Linking (light.exe) ───────────────────────────────────────────────────────
Write-Host "Linkage ..."

$wixobjs = Get-ChildItem -Path $ObjDir -Filter "*.wixobj" |
           Select-Object -ExpandProperty FullName

& $Light @wixobjs `
    -ext $WixUIExt `
    -cultures:fr-fr `
    -b $ProjectRoot `
    -sice:ICE38 -sice:ICE43 -sice:ICE57 -sice:ICE60 `
    -out $OutputMsi `
    -nologo
if ($LASTEXITCODE -ne 0) { throw "light.exe a echoue" }

# ── Companion script: installation with a detailed log ───────────────────────
# msiexec logs nothing by default when the MSI is double-clicked. This .cmd,
# generated next to the MSI, starts the installation with /L*v (a complete verbose
# log) — to be used instead of the MSI when an installation fails silently.
$MsiFileName = Split-Path -Leaf $OutputMsi
$LogScript   = Join-Path $InstallerDir "Installer-avec-log.cmd"
$logScriptContent = @"
@echo off
setlocal
cd /d "%~dp0"
set "MSI=$MsiFileName"
set "LOG=install-$ProductVersion.log"
echo Installation de %MSI% avec journal detaille...
msiexec /i "%MSI%" /L*v "%LOG%"
echo.
echo Journal ecrit dans : %CD%\%LOG%
pause
"@
Set-Content -Path $LogScript -Value $logScriptContent -Encoding ASCII

# ── Result ────────────────────────────────────────────────────────────────────
$size = (Get-Item $OutputMsi).Length / 1MB
Write-Host ""
Write-Host "============================================================"
Write-Host "  MSI cree avec succes !"
Write-Host "  $OutputMsi"
Write-Host ("  Taille : {0:F0} Mo" -f $size)
Write-Host "  Pour installer avec journal detaille : $LogScript"
Write-Host "============================================================"
