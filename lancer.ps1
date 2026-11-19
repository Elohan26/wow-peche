$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "Environnement Python introuvable. Depuis la racine : python -m venv .venv puis .\.venv\Scripts\pip install -r backend\requirements.txt"
    exit 1
}

Start-Process -FilePath $python -ArgumentList "-m","uvicorn","main:app","--host","127.0.0.1","--port","8765" -WorkingDirectory (Join-Path $root "backend")
Start-Process -FilePath "cmd.exe" -ArgumentList "/k","npm run dev" -WorkingDirectory (Join-Path $root "frontend")
Start-Sleep -Seconds 2
Start-Process "http://127.0.0.1:5173"
