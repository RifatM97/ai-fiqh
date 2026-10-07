"""Smoke tests CI runs before any deploy (docs/deployment.md §8).

None of these call a model or an embedding API: a CI runner has no
credentials, and these check what can break *before* a credential would be
used — the app failing to render, the committed index being unusable, or the
quota letting traffic through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_fiqh import index
from ai_fiqh.quota import WINDOW_SECONDS, Quota

ROOT = Path(__file__).resolve().parents[1]


# --- the app ------------------------------------------------------------------


def test_app_renders_without_model_credentials(monkeypatch):
    """The page must load on a runner with no Azure, Voyage or Anthropic keys.

    Rendering makes no model call; a crash here means a broken import, a
    missing file, or a configuration error escaping `require_client`.
    """
    from streamlit.testing.v1 import AppTest

    for var in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
                "AZURE_OPENAI_DEPLOYMENT", "VOYAGE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    at = AppTest.from_file(str(ROOT / "src/ai_fiqh/app.py"), default_timeout=120).run()

    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == "AI-Fiqh"
    assert len(at.tabs) == 3


# --- the committed index ------------------------------------------------------


def test_committed_index_loads_without_reembedding(monkeypatch):
    """The image ships the index from git; its embeddings cache must still hit.

    The cache is trusted only when `embeddings.meta.json` matches the chunks.
    If that file is missing or stale, the app would silently re-embed the whole
    corpus through Voyage on its first question — so any attempt to reach
    Voyage here is a failure, not a slow path.
    """
    def refuse(*_args, **_kwargs):
        raise AssertionError("embeddings cache missed: the app would re-embed via Voyage")

    monkeypatch.setattr(index, "_voyage_client", refuse)

    r = index.Retriever(verbose=False)
    assert len(r.chunks) > 0
    assert r.embeddings.shape[0] == len(r.chunks)


# --- the quota (§4c) ------------------------------------------------------------


def test_quota_admits_up_to_the_limit_then_refuses():
    q = Quota(limit=3)
    results = [q.admit("user-a", now=0.0) for _ in range(5)]

    assert results[:3] == [None, None, None]
    assert results[3] == 60 and results[4] == 60  # minutes until the first slot frees


@pytest.mark.parametrize(("elapsed", "expected_minutes"), [
    (0.0, 60),       # a whole hour left: 60, not 61
    (0.5, 60),       # a partial minute rounds up
    (3540.0, 1),     # exactly one minute left
    (3599.9, 1),     # a fraction of a second left still reads as 1, never 0
])
def test_quota_wait_rounds_up_to_whole_minutes(elapsed, expected_minutes):
    q = Quota(limit=1)
    q.admit("user-a", now=0.0)
    assert q.admit("user-a", now=elapsed) == expected_minutes


def test_quota_is_per_user():
    q = Quota(limit=1)
    assert q.admit("user-a", now=0.0) is None
    assert q.admit("user-b", now=0.0) is None
    assert q.admit("user-a", now=1.0) is not None


def test_quota_window_rolls():
    q = Quota(limit=1)
    assert q.admit("user-a", now=0.0) is None
    assert q.admit("user-a", now=WINDOW_SECONDS - 1) is not None
    assert q.admit("user-a", now=WINDOW_SECONDS + 1) is None


@pytest.mark.parametrize(("limit", "who"), [(3, None), (0, "user-a"), (-1, "user-a")])
def test_quota_never_limits_without_identity_or_when_disabled(limit, who):
    """No identity is the local case; a limit of 0 or less switches it off."""
    q = Quota(limit=limit)
    assert all(q.admit(who, now=0.0) is None for _ in range(10))
