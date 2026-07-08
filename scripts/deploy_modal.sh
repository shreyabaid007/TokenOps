#!/usr/bin/env bash
# Deploy TokenOps v2 to Modal (embedder → proxy → agent scheduler).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Deploy embedder (Qdrant population runs on first request)"
modal deploy modal_app/embedder.py

echo "==> Deploy proxy (FastAPI + agent approval endpoints)"
modal deploy modal_app/proxy_app.py

echo "==> Deploy agent scheduler (cron every 6h)"
modal deploy modal_app/agent_app.py

echo ""
echo "One-time setup (if not done):"
echo "  python scripts/setup_production_db.py"
echo "  modal run modal_app/agent_app.py::setup_checkpointer  # optional if script above ran"
echo ""
echo "Ensure Modal secret 'tokenops-prod' includes:"
echo "  DATABASE_URL, QDRANT_URL, OPENROUTER_API_KEY, AGENT_ADMIN_KEY, ..."
