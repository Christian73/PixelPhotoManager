# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
#
# Wrapper fin autour de launch_isolated.py, pour l'usage manuel interactif :
#   .\tools\test_env\launch_isolated.ps1 -ScanFolder "C:\chemin\vers\photos"
#
# Lance PixelPhotoManager avec un profil %LOCALAPPDATA% isolé et jetable,
# sans jamais toucher au vrai %LOCALAPPDATA%\PixelPhotoManager de l'utilisateur.

param(
    [string[]]$ScanFolder = @(),
    [string]$AppData
)

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"

$PyArgs = @()
foreach ($folder in $ScanFolder) {
    $PyArgs += "--scan-folder"
    $PyArgs += $folder
}
if ($AppData) {
    $PyArgs += "--app-data"
    $PyArgs += $AppData
}

& $PythonExe -m tools.test_env.launch_isolated @PyArgs
