"""Spec for the HTTP surface's translation layer.

app.py contains no business logic — routing lives in supervisor, flow in graph.
What it does own is turning a Meta event into the right graph invocation, and
that mapping is worth pinning because a wrong answer here is silent: the message
simply goes to the wrong thread or nowhere.
"""
import pytest

from winch.app import _parse_contractor_reply
from winch.config import Settings
from winch.webhook import ParsedEvent
from datetime import datetime, timezone


def ev(text=None, button=None):
    return ParsedEvent(
        kind="message", provider_message_id="wamid.x", from_wa_id="447700900001",
        text=text, button_payload=button, timestamp=datetime.now(timezone.utc),
    )


class TestContractorReplyParsing:
    @pytest.mark.parametrize("word", ["yes", "YES", "approve", "send", "ok", "start", "1", " Ok "])
    def test_affirmatives_approve(self, word):
        assert _parse_contractor_reply(ev(text=word)) == {"approved": True}

    @pytest.mark.parametrize("word", ["no", "NO", "hold", "stop", "cancel", "0"])
    def test_negatives_hold(self, word):
        assert _parse_contractor_reply(ev(text=word)) == {"approved": False}

    def test_button_payload_takes_precedence_over_text(self):
        assert _parse_contractor_reply(ev(text="whatever", button="yes")) == {"approved": True}

    def test_free_text_becomes_a_text_payload(self):
        out = _parse_contractor_reply(ev(text="the number is 07700 900412"))
        assert out == {"text": "the number is 07700 900412"}

    def test_never_returns_an_empty_payload(self):
        """Command(resume={}) is silently ignored by LangGraph and the node
        interrupts again, so the graph would look stuck. Every branch here must
        produce a non-empty dict — see test_graph.TestLangGraphGotchas."""
        for event in (ev(text=""), ev(text=None), ev(button=""), ev(text="   ")):
            assert _parse_contractor_reply(event) != {}


class TestSettingsSafety:
    def test_settings_repr_hides_every_secret(self, monkeypatch):
        for k, v in {
            "AZURE_OPENAI_ENDPOINT": "https://e.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "azure-secret",
            "LLM_MODEL": "azure_openai:gpt-4-1-mini",
            "META_ACCESS_TOKEN": "meta-secret",
            "META_APP_SECRET": "app-secret",
            "META_VERIFY_TOKEN": "verify-secret",
            "DATABASE_URL": "postgresql://u:dbsecret@h/d",
        }.items():
            monkeypatch.setenv(k, v)
        text = repr(Settings.from_env())
        for secret in ("azure-secret", "meta-secret", "app-secret",
                       "verify-secret", "dbsecret"):
            assert secret not in text
