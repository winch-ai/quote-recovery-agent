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


class TestTickAuth:
    """Cloud Run auth is per-service, so /internal/tick is public whenever the
    webhook is. The shared secret is the only thing protecting it."""

    def _app(self, monkeypatch, tick_secret):
        for k, v in {
            "AZURE_OPENAI_ENDPOINT": "https://e.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "k", "LLM_MODEL": "azure_openai:m",
        }.items():
            monkeypatch.setenv(k, v)
        if tick_secret is not None:
            monkeypatch.setenv("TICK_SECRET", tick_secret)
        from winch.app import Runtime, _internal_router
        from winch.config import Settings
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        settings = Settings.from_env()
        runtime = Runtime(settings)

        class _EmptyQueue:
            async def claim_due(self, now, limit=20):
                return []

        runtime.queue = _EmptyQueue()   # auth is what is under test, not draining
        app = FastAPI()
        app.include_router(_internal_router(runtime, settings))
        return TestClient(app)

    def test_correct_secret_is_accepted(self, monkeypatch):
        client = self._app(monkeypatch, "s3cret")
        resp = client.post("/internal/tick", headers={"X-Tick-Secret": "s3cret"})
        assert resp.status_code == 200
        assert resp.json() == {"claimed": 0}

    def test_wrong_secret_is_rejected(self, monkeypatch):
        client = self._app(monkeypatch, "s3cret")
        assert client.post("/internal/tick",
                           headers={"X-Tick-Secret": "wrong"}).status_code == 403

    def test_missing_header_is_rejected(self, monkeypatch):
        client = self._app(monkeypatch, "s3cret")
        assert client.post("/internal/tick").status_code == 403

    def test_unset_secret_rejects_everything(self, monkeypatch):
        """Fails closed: a misconfigured deploy is inert, not wide open."""
        monkeypatch.delenv("TICK_SECRET", raising=False)
        client = self._app(monkeypatch, None)
        assert client.post("/internal/tick").status_code == 403
        assert client.post("/internal/tick",
                           headers={"X-Tick-Secret": ""}).status_code == 403

    def test_health_needs_no_secret(self, monkeypatch):
        client = self._app(monkeypatch, "s3cret")
        assert client.get("/health").status_code == 200

    def test_health_is_not_at_healthz(self, monkeypatch):
        """Google's frontend intercepts /healthz on Cloud Run - the request
        never reaches the container, so the endpoint must not live there."""
        client = self._app(monkeypatch, "s3cret")
        assert client.get("/healthz").status_code == 404


class TestLegalPages:
    """Meta will not let an app leave Development mode without a Privacy Policy
    URL. These are served by the app because there is no domain yet."""

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from winch.legal import build_router
        app = FastAPI()
        app.include_router(build_router("ops@example.com", "Example Ltd"))
        return TestClient(app)

    def test_privacy_is_publicly_reachable(self):
        assert self._client().get("/privacy").status_code == 200

    def test_terms_is_publicly_reachable(self):
        assert self._client().get("/terms").status_code == 200

    def test_privacy_names_every_third_party_that_sees_customer_data(self):
        """If a processor is added and not listed here, the policy is false."""
        body = self._client().get("/privacy").text
        for processor in ("Meta", "Azure OpenAI", "Google Cloud"):
            assert processor in body, f"{processor} not disclosed"

    def test_privacy_states_the_stop_route(self):
        body = self._client().get("/privacy").text
        assert "STOP" in body

    def test_contact_email_is_rendered_when_configured(self):
        assert "ops@example.com" in self._client().get("/privacy").text

    def test_no_contact_email_degrades_without_breaking(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from winch.legal import build_router
        app = FastAPI()
        app.include_router(build_router("", "Example Ltd"))
        resp = TestClient(app).get("/privacy")
        assert resp.status_code == 200
        assert "mailto:" not in resp.text


class TestDataDeletionPage:
    """Meta requires a Data Deletion Instructions URL before an app goes Live."""

    def _client(self, email="ops@example.com"):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from winch.legal import build_router
        app = FastAPI()
        app.include_router(build_router(email, "Example Ltd"))
        return TestClient(app)

    def test_page_is_publicly_reachable(self):
        assert self._client().get("/data-deletion").status_code == 200

    def test_stop_is_offered_as_the_fastest_route(self):
        """A route that needs no email and no explanation is the one people use."""
        assert "STOP" in self._client().get("/data-deletion").text

    def test_it_is_honest_about_what_cannot_be_deleted(self):
        """Claiming to delete messages already on someone's phone would be false."""
        body = self._client().get("/data-deletion").text
        assert "Meta's systems" in body

    def test_it_states_a_deletion_deadline(self):
        assert "30 days" in self._client().get("/data-deletion").text


class TestLazyWiring:
    """Collaborators are built in Runtime.start(), which runs in the lifespan
    hook - after create_app() has wired the router. Anything captured by value
    at construction time is captured as None, forever."""

    def _runtime(self, monkeypatch):
        for k, v in {
            "AZURE_OPENAI_ENDPOINT": "https://e.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "k", "LLM_MODEL": "azure_openai:m",
        }.items():
            monkeypatch.setenv(k, v)
        from winch.app import Runtime
        from winch.config import Settings
        return Runtime(Settings.from_env())

    async def test_lazy_deduplicator_resolves_after_start(self, monkeypatch):
        from winch.app import _LazyDeduplicator
        from winch.webhook import InMemoryDeduplicator

        runtime = self._runtime(monkeypatch)
        lazy = _LazyDeduplicator(runtime)          # built while it is still None
        runtime.deduplicator = InMemoryDeduplicator()   # as Runtime.start() does

        assert await lazy.seen("wamid.1") is False
        assert await lazy.seen("wamid.1") is True
        await lazy.release("wamid.1")
        assert await lazy.seen("wamid.1") is False

    async def test_lazy_deduplicator_raises_clearly_before_start(self, monkeypatch):
        """A clear error beats AttributeError on NoneType, which is what shipped."""
        from winch.app import _LazyDeduplicator

        lazy = _LazyDeduplicator(self._runtime(monkeypatch))
        with pytest.raises(RuntimeError, match="Runtime.start"):
            await lazy.seen("wamid.1")
