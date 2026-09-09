$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$venvRoot = Join-Path $projectRoot ".venv"
$windowsPython = Join-Path $venvRoot "Scripts\python.exe"
$alternatePython = Join-Path $venvRoot "bin\python.exe"

if (-not (Test-Path -LiteralPath $windowsPython) -and -not (Test-Path -LiteralPath $alternatePython)) {
    Write-Host "Creating the Python virtual environment..."
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.13 -m venv $venvRoot
    } else {
        python -m venv $venvRoot
    }
}

if (Test-Path -LiteralPath $windowsPython) {
    $pythonExecutable = $windowsPython
} elseif (Test-Path -LiteralPath $alternatePython) {
    $pythonExecutable = $alternatePython
} else {
    throw "The virtual environment was not created successfully."
}

Write-Host "Checking backend dependencies..."
& $pythonExecutable -m pip install --disable-pip-version-check --quiet --requirement (Join-Path $projectRoot "backend\requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}

$environmentFile = Join-Path $projectRoot ".env"
if (-not (Test-Path -LiteralPath $environmentFile)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot ".env.example") -Destination $environmentFile
    Write-Host "Created .env from .env.example. Add Binance Testnet credentials there when needed."
}

Write-Host "Starting the backend and frontend. Press Ctrl+C to stop both."
& $pythonExecutable (Join-Path $projectRoot "backend\run.py")
