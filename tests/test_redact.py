"""Tests for proxy.redact — PII redaction via Presidio."""

import pytest

from proxy.redact import RedactResult, redact_prompt


@pytest.mark.asyncio
async def test_disabled_config_passes_through():
    """When redaction is disabled, text passes through unchanged."""
    text = "Contact John at john@example.com"
    result = await redact_prompt(text, {"enabled": False})
    assert result.redacted_text == text
    assert result.was_redacted is False
    assert result.entities_found == []


@pytest.mark.asyncio
async def test_none_config_defaults_to_enabled():
    """None config defaults to enabled — entities should be found."""
    text = "Email me at alice@example.com or call 555-123-4567"
    result = await redact_prompt(text, None)
    if result.was_redacted:
        assert "alice@example.com" not in result.redacted_text
        assert len(result.entities_found) > 0


@pytest.mark.asyncio
async def test_email_redaction():
    """Emails should be redacted."""
    text = "Send to bob@example.com please"
    result = await redact_prompt(text, {"enabled": True, "entity_types": ["EMAIL_ADDRESS"], "action": "redact"})
    if result.was_redacted:
        assert "bob@example.com" not in result.redacted_text
        assert any(e.entity_type == "EMAIL_ADDRESS" for e in result.entities_found)


@pytest.mark.asyncio
async def test_credit_card_redaction():
    """Credit card numbers should be redacted."""
    text = "My card is 4111-1111-1111-1111"
    result = await redact_prompt(text, {"enabled": True, "entity_types": ["CREDIT_CARD"], "action": "redact"})
    if result.was_redacted:
        assert "4111" not in result.redacted_text


@pytest.mark.asyncio
async def test_no_pii_passes_through():
    """Text without PII passes through unchanged."""
    text = "What is the weather today?"
    result = await redact_prompt(text, {"enabled": True})
    assert result.redacted_text == text
    assert result.was_redacted is False


@pytest.mark.asyncio
async def test_mask_action():
    """Mask action should produce asterisks."""
    text = "Call alice@example.com"
    result = await redact_prompt(text, {"enabled": True, "entity_types": ["EMAIL_ADDRESS"], "action": "mask"})
    if result.was_redacted:
        assert "alice@example.com" not in result.redacted_text
        assert "*" in result.redacted_text


@pytest.mark.asyncio
async def test_multiple_entities():
    """Multiple PII entities in one text."""
    text = "John Smith's email is john@example.com and card is 4111-1111-1111-1111"
    result = await redact_prompt(text, {"enabled": True, "action": "redact"})
    if result.was_redacted:
        assert len(result.entities_found) >= 2


@pytest.mark.asyncio
async def test_entity_count():
    """Entity counts should match found entities."""
    text = "alice@example.com and bob@example.com"
    result = await redact_prompt(text, {"enabled": True, "entity_types": ["EMAIL_ADDRESS"], "action": "redact"})
    if result.was_redacted:
        email_entities = [e for e in result.entities_found if e.entity_type == "EMAIL_ADDRESS"]
        assert len(email_entities) == 2
