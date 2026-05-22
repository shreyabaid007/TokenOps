"""Complexity classifier — labels prompts as low / mid / high.

Two-stage:
  1. Word-count pre-classifier (no LLM call): prompts shorter than
     routing_rules.low_max_tokens words are 'low'; longer than
     high_min_tokens words are 'high'. Bands are agent-tunable so the
     optimizer can widen the cheap tiers as quality evidence accumulates.
  2. Only prompts in the ambiguous middle band invoke claude-haiku-4-5
     as a structured-output judge. Keeps classifier overhead near zero
     on the majority of traffic.

Async because the LLM call is hot-path network I/O. The structured
ComplexityResult is internal; the bare tier string is returned at the
module boundary, and the LLM's `reason` field is logged at DEBUG.
"""

import logging
from typing import Literal

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from proxy import config
from proxy.config import settings

logger = logging.getLogger(__name__)

_CLASSIFIER_MODEL = "anthropic/claude-haiku-4-5"
_PROMPT_CAP_CHARS = 300

_INSTRUCTIONS = (
    "Classify the following LLM prompt by reasoning complexity.\n"
    "- low: simple lookup, paraphrase, or factual question (no multi-step reasoning).\n"
    "- mid: moderate reasoning, summarisation, code review, structured generation.\n"
    "- high: complex multi-step reasoning, deep analysis, open-ended planning.\n"
    "Return the tier and a one-line reason.\n\n"
    "Prompt:\n{prompt}"
)


class ComplexityResult(BaseModel):
    """Structured Haiku output. Constraining `tier` to a Literal forces
    langchain-anthropic's tool-call validator to reject malformed responses
    at parse time rather than letting them reach router.select_model."""

    tier: Literal["low", "mid", "high"]
    reason: str


_chain = None


def _get_chain():
    """Cached LangChain runnable. Lazy so Settings is loaded before
    construction and tests can monkeypatch the chain wholesale."""
    global _chain
    if _chain is None:
        llm = ChatOpenAI(
            model=_CLASSIFIER_MODEL,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            timeout=10,
            max_retries=0,
        )
        _chain = llm.with_structured_output(ComplexityResult)
    return _chain


async def classify(prompt: str) -> Literal["low", "mid", "high"]:
    """Classify reasoning complexity for tier-based routing.

    Word-count short-circuit first; falls back to Haiku LLM-as-judge for
    prompts in the ambiguous middle band. Reads tier bounds from
    config.current_rules at call time so agent updates take effect on the
    very next request. Soft-fails to 'mid' on any classifier exception —
    the proxy keeps serving via Sonnet instead of returning 502.
    """
    rules = config.current_rules
    word_count = len(prompt.split())

    if word_count <= rules.low_max_tokens:
        return "low"
    if word_count >= rules.high_min_tokens:
        return "high"

    capped = prompt[:_PROMPT_CAP_CHARS]
    try:
        result: ComplexityResult = await _get_chain().ainvoke(
            _INSTRUCTIONS.format(prompt=capped)
        )
    except Exception as exc:
        logger.warning(
            "classifier failed — defaulting to mid",
            extra={"error": str(exc)},
        )
        return "mid"

    logger.debug(
        "classifier llm tier",
        extra={"tier": result.tier, "reason": result.reason},
    )
    return result.tier
