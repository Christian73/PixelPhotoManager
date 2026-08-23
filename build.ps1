#Requires -Version 5.1
# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
<#
.SYNOPSIS
    Complete packaging of PixelPhotoManager: PyInstaller EXE then WiX MSI.
.DESCRIPTION
    Step 1 — PyInstaller: produces dist\PixelPhotoManager\ (one-dir)
    Step 2 — WiX v3     : produces installer\PixelPhotoManager-<version>-x64.msi

    Usage:
        .\build.ps1                    # EXE + MSI (asks for the version number)
        .\build.ps1 -Version 1.1.0     # EXE + MSI, version supplied directly
        .\build.ps1 -ExeOnly           # EXE only
        .\build.ps1 -MsiOnly           # MSI only (EXE already built, VERSION already up to date)
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

# ── Version number ────────────────────────────────────────────────────────────
# The single source of truth for this build: the VERSION file at the root of the
# repository, read both by pixelphotomanager.spec (embedded in the exe, read by
# get_app_version() in frozen mode) and by installer\build_msi.ps1 (Product/@Version
# of the MSI). Without -MsiOnly, the number is always asked for again (the latest
# git tag is offered by default) so as to avoid forgetting to update it and
# publishing an exe that still believes it is version N-1.
if (-not $MsiOnly) {
    if (-not $Version) {
        # The latest tag of the repository (not necessarily an ancestor of HEAD: "git describe"
        # would fail here if the tag is not reachable from the current branch,
        # e.g. a tag put on main while building from develop).
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

# ── Step 1: PyInstaller ───────────────────────────────────────────────────────
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

# ── Step 2: MSI ───────────────────────────────────────────────────────────────
if (-not $ExeOnly) {
    Write-Host "`n[2/2] Build MSI..." -ForegroundColor Yellow
    & powershell -ExecutionPolicy Bypass -File ".\installer\build_msi.ps1"
    if ($LASTEXITCODE -ne 0) { Write-Error "build_msi.ps1 a echoue (code $LASTEXITCODE)." }
}

Write-Host "`n=== Build termine ===" -ForegroundColor Cyan
