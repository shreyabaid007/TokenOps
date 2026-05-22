"""Host application — four demo endpoints that exercise the proxy.

Each endpoint takes a user-supplied input string, wraps it in an
endpoint-specific system prompt, and forwards the call to TokenOps via
PROXY_URL. The proxy's response is returned to the caller verbatim with
an added `endpoint` field for downstream debugging.

The host_app exists to generate realistic, varied traffic for the demo —
not to serve real users. Each tag flows through the proxy unchanged and
shows up in the cost ledger keyed by that tag.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from host_app.prompts import SYSTEM_PROMPTS
from proxy.config import settings

logger = logging.getLogger(__name__)


class EndpointRequest(BaseModel):
    input: str


_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """One AsyncClient per process — connection-pool reuse across requests."""
    global _client
    _client = httpx.AsyncClient(timeout=120.0)
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


app = FastAPI(title="TokenOps Host App", lifespan=lifespan)


def _client_or_raise() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("httpx client not initialised — lifespan must run first")
    return _client


async def _call_proxy(tag: str, user_input: str) -> dict[str, object]:
    """Wrap user_input in the endpoint's system context and forward to the proxy.

    Returns the proxy's response dict verbatim with an added `endpoint` key.
    The proxy already validates its own response shape; the host_app is a
    pass-through and does not re-model it.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPTS[tag]},
        {"role": "user", "content": user_input},
    ]
    try:
        response = await _client_or_raise().post(
            f"{settings.proxy_url}/v1/chat/completions",
            json={"messages": messages, "tag": tag},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "proxy call failed",
            extra={"tag": tag, "error": str(exc)},
        )
        raise HTTPException(
            status_code=502,
            detail={"error": "proxy_unreachable", "detail": str(exc)},
        )
    body = response.json()
    body["endpoint"] = tag
    return body


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok"}


@app.post("/sql-analyst")
async def sql_analyst(req: EndpointRequest) -> dict[str, object]:
    return await _call_proxy("sql-analyst", req.input)


@app.post("/code-reviewer")
async def code_reviewer(req: EndpointRequest) -> dict[str, object]:
    return await _call_proxy("code-reviewer", req.input)


@app.post("/log-explainer")
async def log_explainer(req: EndpointRequest) -> dict[str, object]:
    return await _call_proxy("log-explainer", req.input)


@app.post("/doc-writer")
async def doc_writer(req: EndpointRequest) -> dict[str, object]:
    return await _call_proxy("doc-writer", req.input)
