"""Shared pytest fixtures for the TokenOps test suite.

Settings() is instantiated at module load time in proxy/config.py and
will raise on any missing required env var. The fixture below provides
test stand-ins so unit tests can import proxy modules without a real
.env file present.

The os.environ.setdefault calls run at conftest import — which happens
before pytest collects any test module — so a test file that does
`from proxy import config` at the top still finds these values in place.
The fixture handles per-test resets if a test mutates the env.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("MODAL_EMBEDDER_APP", "test-embedder")

import pytest


@pytest.fixture(autouse=True)
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.setenv("MODAL_EMBEDDER_APP", "test-embedder")
