"""Modal scheduled job for the TokenOps optimizer agent.

Deploy:
    modal deploy modal_app/agent_app.py

Runs the LangGraph optimizer on a cron schedule (default: every 6 hours).
"""

from __future__ import annotations

import modal

SPACY_MODEL = "en_core_web_sm"
SPACY_WHEEL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    f"{SPACY_MODEL}-3.7.1/{SPACY_MODEL}-3.7.1-py3-none-any.whl"
)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "psycopg[binary,pool]==3.1.18",
        "pydantic==2.7.1",
        "pydantic-settings==2.2.1",
        "python-dotenv==1.0.1",
        "langgraph==0.2.28",
        "langgraph-checkpoint-postgres==2.0.1",
        "langchain-core==0.3.0",
        "langchain-openai==0.2.0",
        "apscheduler==3.10.4",
        "presidio-analyzer==2.2.354",
        "spacy==3.7.5",
    )
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
    """Single optimizer cycle — observe, propose, interrupt at approval gate."""
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
