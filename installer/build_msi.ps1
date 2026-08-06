#Requires -Version 5.1
# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
<#
.SYNOPSIS
    Construit le MSI Pixel Photo Manager avec WiX Toolset v3.
.DESCRIPTION
    1. Verifie / installe WiX Toolset v3.
    2. Genere les bitmaps de l'installeur (banner.bmp, dialog.bmp).
    3. Lance heat.exe pour inventorier dist\PixelPhotoManager\.
    4. Compile avec candle.exe et lie avec light.exe.
    5. Produit : installer\PixelPhotoManager-<version>-x64.msi

    Lancer depuis le repertoire du projet ou depuis installer\ :
        powershell -ExecutionPolicy Bypass -File installer\build_msi.ps1
#>
$ErrorActionPreference = "Stop"

# ── Chemins ──────────────────────────────────────────────────────────────────
$InstallerDir = $PSScriptRoot
$ProjectRoot  = Split-Path -Parent $InstallerDir
$DistDir      = Join-Path $ProjectRoot "dist\PixelPhotoManager"
$ObjDir       = Join-Path $InstallerDir "obj"
$VersionFile  = Join-Path $ProjectRoot "VERSION"

# ── Numero de version (source unique : VERSION a la racine du depot) ─────────
# Ecrit par build.ps1 avant le build EXE ; si ce script est lance seul (hors
# build.ps1), on le demande ici pour ne pas publier un MSI avec une version
# perimee ou absente.
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

# Nom de sortie versionne (ex: PixelPhotoManager-1.0.1-x64.msi)
$OutputMsi = Join-Path $InstallerDir "PixelPhotoManager-$ProductVersion-x64.msi"

# ── Localiser WiX v3 ─────────────────────────────────────────────────────────
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

# ── Verifications prereqs ─────────────────────────────────────────────────────
if (-not (Test-Path "$DistDir\PixelPhotoManager.exe")) {
    throw "PixelPhotoManager.exe introuvable dans $DistDir.`nLancez d'abord le build PyInstaller : .\build.ps1 -ExeOnly"
}
if (-not (Test-Path (Join-Path $ProjectRoot "assets\app_icon.ico"))) {
    throw "assets\app_icon.ico introuvable."
}

# ── Bitmaps de l'installeur ───────────────────────────────────────────────────
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

# ── Harvest : application ─────────────────────────────────────────────────────
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

# ── Linkage (light.exe) ───────────────────────────────────────────────────────
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

# ── Script compagnon : installation avec journal detaille ────────────────────
# msiexec ne journalise rien par defaut au double-clic sur le MSI. Ce .cmd,
# genere a cote du MSI, lance l'installation avec /L*v (log verbeux complet) —
# a utiliser a la place du MSI quand une installation echoue silencieusement.
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

# ── Resultat ──────────────────────────────────────────────────────────────────
$size = (Get-Item $OutputMsi).Length / 1MB
Write-Host ""
Write-Host "============================================================"
Write-Host "  MSI cree avec succes !"
Write-Host "  $OutputMsi"
Write-Host ("  Taille : {0:F0} Mo" -f $size)
Write-Host "  Pour installer avec journal detaille : $LogScript"
Write-Host "============================================================"
