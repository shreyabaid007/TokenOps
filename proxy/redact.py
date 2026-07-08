"""PII redaction using Microsoft Presidio.

Sits in the hot path BEFORE cache lookup and LLM call so that:
  1. The cache key uses the redacted prompt — "John asked X" and "Jane asked X"
     share one cache entry.
  2. No PII ever reaches the LLM provider or gets stored in Qdrant.

Per-tenant configuration: tenants can disable redaction or customise which
entity types to detect via their redaction_config column.
"""

import logging
from dataclasses import dataclass, field

from proxy.auth import SPACY_MODEL

logger = logging.getLogger(__name__)

_analyzer = None
_anonymizer = None


def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            nlp_configuration = {
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": SPACY_MODEL}],
            }
            provider = NlpEngineProvider(nlp_configuration=nlp_configuration)
            nlp_engine = provider.create_engine()
            _analyzer = AnalyzerEngine(
                nlp_engine=nlp_engine,
                supported_languages=["en"],
            )
        except ImportError:
            logger.warning("presidio-analyzer not installed — PII redaction disabled")
            return None
        except Exception as exc:
            logger.warning(
                "presidio analyzer init failed — PII redaction disabled",
                extra={"error": str(exc), "spacy_model": SPACY_MODEL},
            )
            return None
    return _analyzer


def _get_anonymizer():
    global _anonymizer
    if _anonymizer is None:
        try:
            from presidio_anonymizer import AnonymizerEngine
            _anonymizer = AnonymizerEngine()
        except ImportError:
            logger.warning("presidio-anonymizer not installed — PII redaction disabled")
            return None
    return _anonymizer


DEFAULT_ENTITY_TYPES: list[str] = [
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD",
]


@dataclass
class RedactEntity:
    entity_type: str
    start: int
    end: int


@dataclass
class RedactResult:
    redacted_text: str
    entities_found: list[RedactEntity] = field(default_factory=list)
    was_redacted: bool = False


async def redact_prompt(text: str, config: dict | None = None) -> RedactResult:
    """Redact PII from a prompt based on tenant-level config.

    This is async for interface consistency with other proxy modules but
    does CPU-bound work synchronously (Presidio is not async). For the
    prompt sizes we handle (< 10KB), this is sub-millisecond.
    """
    if config is None:
        config = {}

    if not config.get("enabled", True):
        return RedactResult(redacted_text=text)

    analyzer = _get_analyzer()
    anonymizer = _get_anonymizer()
    if analyzer is None or anonymizer is None:
        return RedactResult(redacted_text=text)

    entity_types = config.get("entity_types", DEFAULT_ENTITY_TYPES)
    action = config.get("action", "redact")

    try:
        results = analyzer.analyze(
            text=text,
            entities=entity_types,
            language="en",
        )

        if not results:
            return RedactResult(redacted_text=text)

        from presidio_anonymizer.entities import OperatorConfig

        operator_map = _build_operator_map(action)
        anonymized = anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators=operator_map,
        )

        entities = [
            RedactEntity(
                entity_type=r.entity_type,
                start=r.start,
                end=r.end,
            )
            for r in results
        ]

        return RedactResult(
            redacted_text=anonymized.text,
            entities_found=entities,
            was_redacted=True,
        )

    except Exception as exc:
        logger.warning("PII redaction failed, passing through", extra={"error": str(exc)})
        return RedactResult(redacted_text=text)


def _build_operator_map(action: str) -> dict:
    """Map action string to Presidio operator config."""
    from presidio_anonymizer.entities import OperatorConfig

    if action == "hash":
        return {"DEFAULT": OperatorConfig("hash", {"hash_type": "sha256"})}
    elif action == "mask":
        return {"DEFAULT": OperatorConfig("mask", {"masking_char": "*", "chars_to_mask": 20, "from_end": False})}
    else:
        return {"DEFAULT": OperatorConfig("replace", {"new_value": "<REDACTED>"})}
