"""Modal deployment for the TokenOps v2 proxy (FastAPI).

Deploy:
    modal deploy modal_app/proxy_app.py

Includes proxy + agent packages (agent endpoints import agent.graph).
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
        "fastapi==0.111.0",
        "uvicorn[standard]==0.29.0",
        "httpx==0.27.0",
        "psycopg[binary,pool]==3.1.18",
        "qdrant-client==1.9.1",
        "pydantic==2.7.1",
        "pydantic-settings==2.2.1",
        "python-dotenv==1.0.1",
        "presidio-analyzer==2.2.354",
        "presidio-anonymizer==2.2.354",
        "spacy==3.7.5",
        "prometheus-client==0.20.0",
        "langgraph==0.2.28",
        "langgraph-checkpoint-postgres==2.0.1",
        "langchain-core==0.3.0",
        "langchain-openai==0.2.0",
    )
    .pip_install(SPACY_WHEEL)
    .add_local_python_source("proxy", "agent")
)

app = modal.App("tokenops-proxy", image=image)


@app.function(
    secrets=[modal.Secret.from_name("tokenops-prod")],
    min_containers=1,
    timeout=300,
)
@modal.asgi_app()
def fastapi_app():
    from proxy.main import app as fastapi_application

    return fastapi_application
