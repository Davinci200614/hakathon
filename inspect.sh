#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT/nouba-agriflow/backend"
exec .venv/bin/python -m scripts.inspect_chat "$@"
