# build.ps1 — Packaging PixelPhotoManager en EXE autonome
# Usage : .\build.ps1
# Prérequis : .venv créé avec pip install -r requirements.txt

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$VENV   = ".\.venv\Scripts"
$SPEC   = "pixelphotomanager.spec"
$DIST   = ".\dist\PixelPhotoManager"

Write-Host "=== PixelPhotoManager — Build EXE ===" -ForegroundColor Cyan

# Vérifications préalables
if (-not (Test-Path "$VENV\python.exe")) {
    Write-Host "ERREUR : venv introuvable. Créez-le avec :" -ForegroundColor Red
    Write-Host "  python -m venv .venv && .\.venv\Scripts\pip install -r requirements.txt"
    exit 1
}

if (-not (Test-Path $SPEC)) {
    Write-Host "ERREUR : $SPEC introuvable." -ForegroundColor Red
    exit 1
}

# Nettoyage des builds précédents
Write-Host "`n[1/3] Nettoyage..." -ForegroundColor Yellow
if (Test-Path ".\build") { Remove-Item ".\build" -Recurse -Force }
if (Test-Path $DIST)     { Remove-Item $DIST     -Recurse -Force }

# Build PyInstaller
Write-Host "`n[2/3] Build PyInstaller (peut prendre 2-5 minutes)..." -ForegroundColor Yellow
& "$VENV\python.exe" -m PyInstaller $SPEC --clean --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERREUR : PyInstaller a échoué (code $LASTEXITCODE)." -ForegroundColor Red
    exit $LASTEXITCODE
}

# Résumé
Write-Host "`n[3/3] Build terminé." -ForegroundColor Green
$size = (Get-ChildItem $DIST -Recurse | Measure-Object -Property Length -Sum).Sum
Write-Host "  Dossier : $DIST"
Write-Host "  Taille  : $([math]::Round($size / 1MB, 0)) Mo"
Write-Host ""
Write-Host "Pour distribuer : compressez le dossier $DIST en ZIP." -ForegroundColor Cyan
