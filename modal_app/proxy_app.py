"""Modal deployment for the TokenOps v2 proxy (FastAPI).

Deploy:
    modal deploy modal_app/proxy_app.py

Includes proxy + agent packages (agent approval endpoints import agent.graph).
The image installs the exact pins from requirements.txt plus the spaCy NER
model Presidio needs.
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
