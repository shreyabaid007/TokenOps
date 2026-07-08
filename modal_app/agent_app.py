"""Modal scheduled job for the TokenOps optimizer agent.

Deploy:
    modal deploy modal_app/agent_app.py

Runs the LangGraph optimizer on a cron schedule (every 6 hours). Each run
pauses at the approval gate; review via GET /v1/agent/pending and
POST /v1/agent/approve on the proxy.
"""

from __future__ import annotations

from pathlib import Path

import modal

REPO_ROOT = Path(__file__).resolve().parent.parent

SPACY_WHEEL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl"
)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements(str(REPO_ROOT / "requirements.txt"))
    .pip_install(SPACY_WHEEL)
    .add_local_python_source("proxy", "agent")
)

app = modal.App("tokenops-agent", image=image)


@app.function(
    secrets=[modal.Secret.from_name("tokenops-prod")],
    schedule=modal.Cron("0 */6 * * *"),
    timeout=600,
)
def run_optimizer():
    """Single optimizer cycle — observe, propose, pause at approval gate."""
    from agent.graph import run_optimizer as _run

    return _run()


@app.function(
    secrets=[modal.Secret.from_name("tokenops-prod")],
    timeout=120,
)
def setup_checkpointer():
    """One-time: create LangGraph checkpoint tables. Run after db/schema.sql."""
    from agent.graph import setup_checkpointer as _setup

    _setup()
    return {"status": "ok", "message": "LangGraph checkpointer tables ready"}
