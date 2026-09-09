#!/usr/bin/env bash
# One-command launcher for Linux/macOS, mirroring start.ps1 on Windows.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv_root="$project_root/.venv"
python_executable="$venv_root/bin/python"

if [[ ! -x "$python_executable" ]]; then
  echo "Creating the Python virtual environment..."
  if python3.13 -m venv "$venv_root" 2>/dev/null || python3 -m venv "$venv_root" 2>/dev/null; then
    :
  else
    rm -rf "$venv_root"
    python_executable="$(command -v python3)"
    echo "Venv creation is not allowed here; falling back to system Python ($python_executable)."
  fi
fi

echo "Checking backend dependencies..."
"$python_executable" -m pip install --disable-pip-version-check --quiet \
  --requirement "$project_root/backend/requirements.txt"

if [[ ! -f "$project_root/.env" ]]; then
  cp "$project_root/.env.example" "$project_root/.env"
  echo "Created .env from .env.example. Add Binance credentials there when needed."
fi

echo "Starting the backend and frontend. Press Ctrl+C to stop both."
exec "$python_executable" "$project_root/backend/run.py"
