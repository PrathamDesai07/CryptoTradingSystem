$ErrorActionPreference = "Stop"

$frontendRoot = Join-Path $PSScriptRoot "frontend"

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "Node.js and npm are required to run the frontend. Install Node.js 18 or newer."
}

Push-Location $frontendRoot
try {
    if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot "node_modules"))) {
        Write-Host "Installing frontend dependencies..."
        npm install --include=dev
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend dependency installation failed."
        }
    }

    Write-Host "Starting the frontend at http://localhost:3000. Press Ctrl+C to stop."
    npm run dev
} finally {
    Pop-Location
}
