$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

& "$ScriptDir\.venv\Scripts\python.exe" "$ScriptDir\main.py"

if ($LASTEXITCODE -ne 0) {
    $LogPath = "$ScriptDir\logs\photomanager.log"
    Write-Host ""
    Write-Host "L'application s'est terminee avec le code $LASTEXITCODE." -ForegroundColor Red
    if (Test-Path $LogPath) {
        Write-Host "Dernières lignes du log :" -ForegroundColor Yellow
        Get-Content $LogPath -Tail 30
    }
    Write-Host ""
    Write-Host "Appuyez sur une touche pour fermer..." -ForegroundColor Gray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}
