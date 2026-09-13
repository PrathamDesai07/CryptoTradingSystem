#!/usr/bin/env bash
# Starts the Vite frontend as a separate process from the backend.
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
frontend_root="$project_root/frontend"

if ! command -v npm >/dev/null 2>&1; then
  echo "Node.js and npm are required to run the frontend. Install Node.js 18 or newer." >&2
  exit 1
fi

cd "$frontend_root"
if [[ ! -d node_modules ]]; then
  echo "Installing frontend dependencies..."
  npm install --include=dev
fi

echo "Starting the frontend at http://localhost:3000. Press Ctrl+C to stop."
exec npm run dev
