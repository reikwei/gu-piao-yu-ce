$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($env:KLINE_DB_PATH)) {
	$env:KLINE_DB_PATH = "data/candles.db"
}

$python = if (Test-Path ".\\.venv\\Scripts\\python.exe") { ".\\.venv\\Scripts\\python.exe" } else { "python" }
& $python -m kronos_mvp.cli serve --host 127.0.0.1 --port 8000
