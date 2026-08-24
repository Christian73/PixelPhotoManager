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
$Torch    = Join-Path $WixBin "torch.exe"
$WixUIExt = Join-Path $WixBin "WixUIExtension.dll"
# WixUtilExtension: the WixShellExec custom action, which starts the application
# from the final screen of the installer (cf. product.wxs).
$WixUtilExt = Join-Path $WixBin "WixUtilExtension.dll"
Write-Host "WiX v3 : $WixBin"

# ── Supported languages ──────────────────────────────────────────────────────
# Same three languages as the application (src/core/i18n.py): English is the
# source language AND the fallback, so it is the base package and gets no
# transform. Any other machine language falls back to it on its own.
# An [ordered] hashtable so the order of the passes stays reproducible.
$BaseLcid       = 1033                                    # en-us
$LangTransforms = [ordered]@{ "fr-fr" = 1036; "de-de" = 1031 }

# ── Prerequisite checks ───────────────────────────────────────────────────────
if (-not (Test-Path "$DistDir\PixelPhotoManager.exe")) {
    throw "PixelPhotoManager.exe introuvable dans $DistDir.`nLancez d'abord le build PyInstaller : .\build.ps1 -ExeOnly"
}
if (-not (Test-Path (Join-Path $ProjectRoot "assets\app_icon.ico"))) {
    throw "assets\app_icon.ico introuvable."
}

# ── Installer bitmaps ─────────────────────────────────────────────────────────
# Regenerated at EVERY build, not only when they are missing: dialog.bmp carries
# the version number in its pixels, so a bitmap kept from a previous build makes
# the installer display the version of that build. It costs a fraction of a second.
$BannerBmp = Join-Path $InstallerDir "banner.bmp"
$DialogBmp = Join-Path $InstallerDir "dialog.bmp"

Write-Host "Generation des bitmaps de l'installeur (version $ProductVersion) ..."
$venvPython   = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$createScript = Join-Path $InstallerDir "create_bitmaps.py"
& $venvPython $createScript $ProductVersion
if ($LASTEXITCODE -ne 0) { throw "create_bitmaps.py a echoue." }
if (-not (Test-Path $BannerBmp) -or -not (Test-Path $DialogBmp)) {
    throw "banner.bmp / dialog.bmp absents apres create_bitmaps.py."
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

# -sw1077: WixShellExecTarget deliberately holds an unresolved [INSTALLFOLDER]
# reference, formatted at run time by WixShellExec (cf. product.wxs).
& $Candle -arch x64 -out "$ObjDir\" -nologo -sw1077 -ext $WixUIExt -ext $WixUtilExt @candleDefs @wxsSources
if ($LASTEXITCODE -ne 0) { throw "candle.exe a echoue" }

# -- Linking (light.exe), one pass per culture ---------------------------------
# The three MSIs are linked from the SAME wixobjs: they differ only by the strings
# resolved from installer\loc\<culture>.wxl. The English one is the base package,
# the other two only exist to be turned into language transforms just below.
#
# -cc/-reusecab shares one cabinet between the three passes: the payload is
# identical, so compressing ~1 GB three times would be pure waste (and -sval skips
# the ICE validation of the two intermediates, only the base one is validated).
Write-Host "Linkage ..."

$wixobjs = Get-ChildItem -Path $ObjDir -Filter "*.wixobj" |
           Select-Object -ExpandProperty FullName

$CabCache = Join-Path $ObjDir "cabcache"
New-Item -ItemType Directory -Force -Path $CabCache | Out-Null

$LocDir = Join-Path $InstallerDir "loc"

# The licence is a file, not a string: it goes through a WiX variable defined here
# rather than through the .wxl (cf. the comment in product.wxs). The path stays
# relative to $ProjectRoot, resolved by -b like the other binaries of the UI.
function License-Arg([string]$Culture) { "-dWixUILicenseRtf=installer\license\$Culture.rtf" }

& $Light @wixobjs `
    -ext $WixUIExt -ext $WixUtilExt `
    -cultures:en-us -loc "$LocDir\en-us.wxl" `
    (License-Arg "en-us") `
    -cc $CabCache `
    -b $ProjectRoot `
    -sice:ICE38 -sice:ICE43 -sice:ICE57 -sice:ICE60 `
    -out $OutputMsi `
    -nologo
if ($LASTEXITCODE -ne 0) { throw "light.exe a echoue (en-us)" }

# The DTF assembly (the same API msiexec uses) serves for the rest of the script.
Add-Type -Path (Join-Path $WixBin "Microsoft.Deployment.WindowsInstaller.dll")

# Product Id="*" makes light draw a NEW ProductCode at every link, so the three
# passes come out with three different ones and torch would put that difference
# into the transform: a French machine would install the same build under another
# ProductCode than an English one. The GUID must stay generated (a fixed one would
# break the MajorUpgrade of the next versions), so it is the language MSIs that are
# realigned on the base one, just before the comparison.
function Get-MsiProductCode([string]$Path) {
    $d = New-Object Microsoft.Deployment.WindowsInstaller.Database(
           $Path, [Microsoft.Deployment.WindowsInstaller.DatabaseOpenMode]::ReadOnly)
    try { return $d.ExecuteScalar("SELECT `Value` FROM `Property` WHERE `Property`='ProductCode'") }
    finally { $d.Close() }
}

$BaseProductCode = Get-MsiProductCode $OutputMsi
Write-Host "  ProductCode de reference : $BaseProductCode"

foreach ($culture in $LangTransforms.Keys) {
    $langMsi = Join-Path $ObjDir "$culture.msi"
    & $Light @wixobjs `
        -ext $WixUIExt -ext $WixUtilExt `
        -cultures:$culture -loc "$LocDir\$culture.wxl" `
        (License-Arg $culture) `
        -cc $CabCache -reusecab -sval `
        -b $ProjectRoot `
        -out $langMsi `
        -nologo
    if ($LASTEXITCODE -ne 0) { throw "light.exe a echoue ($culture)" }

    $dl = New-Object Microsoft.Deployment.WindowsInstaller.Database(
            $langMsi, [Microsoft.Deployment.WindowsInstaller.DatabaseOpenMode]::Transact)
    try {
        $dl.Execute("UPDATE `Property` SET `Value`='$BaseProductCode' WHERE `Property`='ProductCode'")
        $dl.Commit()
    } finally { $dl.Close() }
}

# -- Language transforms (torch.exe) ------------------------------------------
# torch -t language produces the difference between the base MSI and a localized
# one, with the validation flags of a language transform.
Write-Host "Generation des transformations de langue ..."

foreach ($culture in $LangTransforms.Keys) {
    $lcid    = $LangTransforms[$culture]
    $langMsi = Join-Path $ObjDir "$culture.msi"
    $mst     = Join-Path $ObjDir "$lcid.mst"
    & $Torch -nologo -p -t language $OutputMsi $langMsi -out $mst
    if ($LASTEXITCODE -ne 0) { throw "torch.exe a echoue ($culture -> $lcid)" }
}

# -- Embedding the transforms + declaring the languages -----------------------
# The transform is embedded as a substorage named after its LCID, with no
# extension: that is exactly the name Windows Installer looks up ("Looking for
# storage transform: 1036" in an msiexec log) when the Languages summary property
# lists several LCIDs. Naming it "1036.mst" makes the lookup fail with 1624.
Write-Host "Integration des transformations dans le MSI ..."

$db = New-Object Microsoft.Deployment.WindowsInstaller.Database(
        $OutputMsi, [Microsoft.Deployment.WindowsInstaller.DatabaseOpenMode]::Transact)
try {
    $view = $db.OpenView('SELECT `Name`,`Data` FROM `_Storages`')
    $view.Execute()
    foreach ($culture in $LangTransforms.Keys) {
        $lcid = $LangTransforms[$culture]
        $mst  = Join-Path $ObjDir "$lcid.mst"
        $rec  = New-Object Microsoft.Deployment.WindowsInstaller.Record(2)
        $rec.SetString(1, "$lcid")
        $rec.SetStream(2, $mst)
        $view.Assign($rec)
        Write-Host "  transformation $culture -> substorage $lcid"
    }
    $view.Close()
    $db.Commit()
} finally {
    $db.Close()
}

# The Languages summary property is what triggers the automatic selection. It is
# written after the link (light only knows the language of the pass it is running)
# and the platform part of the template is kept as it is.
$si = New-Object Microsoft.Deployment.WindowsInstaller.SummaryInfo($OutputMsi, $true)
$platform = ($si.Template -split ';')[0]
$allLcids = @($BaseLcid) + @($LangTransforms.Values)
$si.Template = "$platform;" + ($allLcids -join ',')
$si.Persist()
$si.Close()
Write-Host "  langues declarees : $($allLcids -join ', ')"

# -- Self-check ---------------------------------------------------------------
# A missing substorage or an incomplete Languages list is invisible until an
# installation on a French or German machine comes out in English.
$check = New-Object Microsoft.Deployment.WindowsInstaller.Database(
           $OutputMsi, [Microsoft.Deployment.WindowsInstaller.DatabaseOpenMode]::ReadOnly)
try {
    $stored = @()
    $vc = $check.OpenView('SELECT `Name` FROM `_Storages`')
    $vc.Execute()
    while ($r = $vc.Fetch()) { $stored += $r.GetString(1) }
    $vc.Close()
    foreach ($lcid in $LangTransforms.Values) {
        if ($stored -notcontains "$lcid") { throw "Transformation $lcid absente du MSI." }
    }
    $tmpl = (New-Object Microsoft.Deployment.WindowsInstaller.SummaryInfo($OutputMsi, $false)).Template
    # Exact comparison of the tokens rather than a search inside the string: an LCID
    # is a substring of nothing else here, but a partial match would silently accept
    # a truncated list.
    $declared = ($tmpl -split ';')[1] -split ','
    foreach ($lcid in $allLcids) {
        if ($declared -notcontains "$lcid") { throw "Langue $lcid absente du sommaire ($tmpl)." }
    }
    Write-Host "  verification OK (substorages : $($stored -join ', ') / sommaire : $tmpl)"
} finally {
    $check.Close()
}

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
