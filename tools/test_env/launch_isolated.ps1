# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
#
# A thin wrapper around launch_isolated.py, for interactive manual use:
#   .\tools\test_env\launch_isolated.ps1 -ScanFolder "C:\path\to\photos"
#
# Launches PixelPhotoManager with an isolated, disposable %LOCALAPPDATA% profile,
# without ever touching the user's real %LOCALAPPDATA%\PixelPhotoManager.

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
