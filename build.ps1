#Requires -Version 5.1
# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
<#
.SYNOPSIS
    Packaging complet de PixelPhotoManager : EXE PyInstaller puis MSI WiX.
.DESCRIPTION
    Etape 1 — PyInstaller : produit dist\PixelPhotoManager\ (one-dir)
    Etape 2 — WiX v3     : produit installer\PixelPhotoManager-Setup-<version>.msi

    Usage :
        .\build.ps1                    # EXE + MSI (demande le numero de version)
        .\build.ps1 -Version 1.1.0     # EXE + MSI, version fournie directement
        .\build.ps1 -ExeOnly           # EXE uniquement
        .\build.ps1 -MsiOnly           # MSI uniquement (EXE deja construit, VERSION deja a jour)
#>
param(
    [switch]$ExeOnly,
    [switch]$MsiOnly,
    [string]$Version
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$VENV = ".\.venv\Scripts"
$SPEC = "pixelphotomanager.spec"
$DIST = ".\dist\PixelPhotoManager"
$VersionFile = ".\VERSION"

Write-Host "=== PixelPhotoManager — Build ===" -ForegroundColor Cyan

# ── Numero de version ──────────────────────────────────────────────────────────
# Source unique de verite pour ce build : le fichier VERSION a la racine du
# depot, lu a la fois par pixelphotomanager.spec (embarque dans l'exe, lu par
# get_app_version() en mode fige) et par installer\build_msi.ps1 (Product/@Version
# du MSI). Sans -MsiOnly, on redemande toujours le numero (le dernier tag git
# est propose par defaut) pour eviter d'oublier de le mettre a jour et de
# publier un exe qui se croit encore en version N-1.
if (-not $MsiOnly) {
    if (-not $Version) {
        # Le dernier tag du depot (pas forcement un ancetre de HEAD : "git describe"
        # echouerait ici si le tag n'est pas atteignable depuis la branche courante,
        # ex. tag pose sur main alors qu'on builde depuis develop).
        $gitTag = git tag --sort=-v:refname 2>$null | Select-Object -First 1
        $suggested = if ($gitTag) {
            $gitTag.TrimStart("v", "V")
        } elseif (Test-Path $VersionFile) {
            (Get-Content $VersionFile -Raw).Trim()
        } else {
            ""
        }
        $prompt = "Numero de version pour ce build (ex: 1.1.0)"
        if ($suggested) { $prompt += " [$suggested]" }
        $input = Read-Host $prompt
        $Version = if ($input) { $input } else { $suggested }
    }
    if ($Version -notmatch '^\d+\.\d+\.\d+$') {
        Write-Error "Numero de version invalide : '$Version' (format attendu : Major.Minor.Build, ex: 1.1.0)"
    }
    Set-Content -Path $VersionFile -Value $Version -NoNewline
    Write-Host "Version : $Version (ecrite dans $VersionFile)" -ForegroundColor Green
} elseif (-not (Test-Path $VersionFile)) {
    Write-Error "$VersionFile introuvable. Lancez d'abord un build EXE (sans -MsiOnly) pour le generer."
}

# ── Etape 1 : PyInstaller ─────────────────────────────────────────────────────
if (-not $MsiOnly) {
    Write-Host "`n[1/2] Build PyInstaller..." -ForegroundColor Yellow

    if (-not (Test-Path "$VENV\python.exe")) {
        Write-Error "venv introuvable. Lancez : python -m venv .venv && .\.venv\Scripts\pip install -r requirements.txt"
    }
    if (-not (Test-Path $SPEC)) {
        Write-Error "$SPEC introuvable."
    }

    if (Test-Path ".\build") { Remove-Item ".\build" -Recurse -Force }
    if (Test-Path $DIST)     { Remove-Item $DIST     -Recurse -Force }

    & "$VENV\python.exe" -m PyInstaller $SPEC --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller a echoue (code $LASTEXITCODE)." }

    $size = (Get-ChildItem $DIST -Recurse | Measure-Object -Property Length -Sum).Sum
    Write-Host "  EXE : $DIST  ($([math]::Round($size / 1MB, 0)) Mo)" -ForegroundColor Green
}

# ── Etape 2 : MSI ─────────────────────────────────────────────────────────────
if (-not $ExeOnly) {
    Write-Host "`n[2/2] Build MSI..." -ForegroundColor Yellow
    & powershell -ExecutionPolicy Bypass -File ".\installer\build_msi.ps1"
    if ($LASTEXITCODE -ne 0) { Write-Error "build_msi.ps1 a echoue (code $LASTEXITCODE)." }
}

Write-Host "`n=== Build termine ===" -ForegroundColor Cyan
