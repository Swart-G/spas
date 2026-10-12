#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export UV_CACHE_DIR=/workspace/.cache/uv
uv sync --frozen --group integration
uv run --frozen --group integration ruff check src tests scripts
uv run --frozen --group integration ruff format --check src tests scripts
uv run --frozen --group integration pytest -q -m 'not live'
uv run --frozen --group integration python scripts/dev.py --smoke --port 0 --gateway-port 0
