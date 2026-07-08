"""Evaluator functions for the TokenOps quality pipeline.

Three evaluators:
  - routing_correctness  deterministic tier comparison, no LLM
  - response_quality     LLM-as-judge (G-Eval style) with score + reasoning
  - faithfulness         is the response grounded in the provided context?

The LLM-based evaluators run sync (offline tooling — async is only required
in the proxy). Both soft-fail to a neutral score on judge errors so a flaky
judge never blocks an eval run outright; failures are logged and surfaced
in the score reasoning.
"""

import logging
from typing import Literal

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_JUDGE_MODEL = "anthropic/claude-haiku-4-5"

TIER_ORDER: dict[str, int] = {"low": 0, "mid": 1, "high": 2}


class QualityScore(BaseModel):
    """LLM-as-judge output for response quality."""

    score: float = Field(ge=0.0, le=1.0, description="Overall quality 0-1")
    reasoning: str = Field(description="One-paragraph justification")


class _JudgeVerdict(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str


_judge_chain = None


def _get_judge():
    global _judge_chain
    if _judge_chain is None:
        # Settings are imported lazily so the deterministic evaluators
        # (routing_correctness) work in environments without a .env —
        # notably the CI gate, which never calls the LLM judges.
        from proxy.config import settings

        llm = ChatOpenAI(
            model=_JUDGE_MODEL,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            timeout=30,
            max_retries=1,
        )
        _judge_chain = llm.with_structured_output(_JudgeVerdict)
    return _judge_chain


def routing_correctness(
    prompt: str,
    assigned_tier: Literal["low", "mid", "high"],
    expected_tier: Literal["low", "mid", "high"],
) -> float:
    """Deterministic tier-match score. No LLM call.

    1.0 for an exact match, 0.5 for an adjacent tier (off by one),
    0.0 for the opposite end of the scale. Adjacent misses get partial
    credit because a mid prompt served by Opus is wasteful but not wrong,
    and a mid prompt served by Haiku often still succeeds.
    """
    distance = abs(TIER_ORDER[assigned_tier] - TIER_ORDER[expected_tier])
    if distance == 0:
        return 1.0
    if distance == 1:
        return 0.5
    return 0.0


_QUALITY_INSTRUCTIONS = (
    "You are a strict quality evaluator. Score the RESPONSE to the PROMPT "
    "on a 0-1 scale considering correctness, completeness, and clarity.\n"
    "{reference_block}"
    "Scoring guide: 1.0 = fully correct and complete; 0.7 = correct with "
    "minor gaps; 0.4 = partially correct or significant omissions; "
    "0.0 = wrong or off-topic.\n\n"
    "PROMPT:\n{prompt}\n\nRESPONSE:\n{response}\n"
)


def response_quality(
    prompt: str,
    response: str,
    reference: str | None = None,
) -> QualityScore:
    """LLM-as-judge quality score with reasoning (G-Eval style).

    When a reference answer is available the judge scores against it;
    otherwise it scores on intrinsic correctness/completeness.
    """
    reference_block = (
        f"A REFERENCE answer is provided — score how well the response "
        f"matches its substance (wording may differ):\nREFERENCE:\n{reference}\n\n"
        if reference
        else ""
    )
    try:
        verdict: _JudgeVerdict = _get_judge().invoke(
            _QUALITY_INSTRUCTIONS.format(
                reference_block=reference_block,
                prompt=prompt[:2000],
                response=response[:4000],
            )
        )
        return QualityScore(score=verdict.score, reasoning=verdict.reasoning)
    except Exception as exc:
        logger.warning("response_quality judge failed", extra={"error": str(exc)})
        return QualityScore(score=0.5, reasoning=f"judge unavailable: {exc}")


_FAITHFULNESS_INSTRUCTIONS = (
    "You are a faithfulness evaluator for RAG outputs. Score 0-1 how well "
    "the RESPONSE is grounded in the CONTEXT: every factual claim in the "
    "response must be supported by the context. Penalise claims that "
    "contradict or go beyond the context.\n"
    "1.0 = fully grounded; 0.5 = mostly grounded with some unsupported "
    "claims; 0.0 = contradicts or ignores the context.\n\n"
    "PROMPT:\n{prompt}\n\nCONTEXT:\n{context}\n\nRESPONSE:\n{response}\n"
)


def faithfulness(prompt: str, response: str, context: str) -> float:
    """Groundedness score for RAG workloads: is the response supported by
    the provided context? Returns the bare score; reasoning is logged at
    DEBUG."""
    try:
        verdict: _JudgeVerdict = _get_judge().invoke(
            _FAITHFULNESS_INSTRUCTIONS.format(
                prompt=prompt[:2000],
                context=context[:4000],
                response=response[:4000],
            )
        )
        logger.debug("faithfulness reasoning", extra={"reasoning": verdict.reasoning})
        return verdict.score
    except Exception as exc:
        logger.warning("faithfulness judge failed", extra={"error": str(exc)})
        return 0.5
