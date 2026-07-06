#Requires -Version 5.1
# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
<#
.SYNOPSIS
    Packaging complet de PixelPhotoManager : EXE PyInstaller puis MSI WiX.
.DESCRIPTION
    Etape 1 — PyInstaller : produit dist\PixelPhotoManager\ (one-dir)
    Etape 2 — WiX v3     : produit installer\PixelPhotoManager-Setup.msi

    Usage :
        .\build.ps1           # EXE + MSI
        .\build.ps1 -ExeOnly  # EXE uniquement
        .\build.ps1 -MsiOnly  # MSI uniquement (EXE deja construit)
#>
param(
    [switch]$ExeOnly,
    [switch]$MsiOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$VENV = ".\.venv\Scripts"
$SPEC = "pixelphotomanager.spec"
$DIST = ".\dist\PixelPhotoManager"

Write-Host "=== PixelPhotoManager — Build ===" -ForegroundColor Cyan

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
