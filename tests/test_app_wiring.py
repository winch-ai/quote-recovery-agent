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
    """_parse_contractor_reply is pure plumbing: it passes the contractor's
    raw text through unchanged and interprets nothing. Interpretation - what
    did they mean by "yes", or "check in now", or "no thanks" - is genuine
    language understanding and belongs to the LLM worker inside the node that
    asked the question (nodes.await_confirm / nodes.await_gate call
    deps.llm.classify_contractor_reply), not to a keyword list at the
    transport boundary. Keyword matching here was the actual production
    defect: anything other than an exact "yes"/"approve"/etc. string was
    silently treated as a decline.
    """

    def test_text_passes_through_unchanged(self):
        assert _parse_contractor_reply(ev(text="check in now")) == {"text": "check in now"}

    def test_an_exact_keyword_also_just_passes_through(self):
        """No special-casing for "yes" either - it goes through the same
        classifier as everything else."""
        assert _parse_contractor_reply(ev(text="yes")) == {"text": "yes"}

    def test_button_payload_takes_precedence_over_text(self):
        assert _parse_contractor_reply(ev(text="whatever", button="yes")) == {"text": "yes"}

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


class TestWindowOpenDelegatesCorrectly:
    """The real production bug: _window_open queried event_type=customer_replied,
    an event that is only ever written for end customers, so the contractor's
    own free-form notifications were always silently refused. It now delegates
    to Runtime.contact_window, which is a plain per-number timestamp."""

    def _runtime(self, monkeypatch):
        for k, v in {
            "AZURE_OPENAI_ENDPOINT": "https://e.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "k", "LLM_MODEL": "azure_openai:m",
        }.items():
            monkeypatch.setenv(k, v)
        from winch.app import Runtime
        from winch.config import Settings
        return Runtime(Settings.from_env())

    async def test_delegates_to_contact_window(self, monkeypatch):
        runtime = self._runtime(monkeypatch)

        class FakeWindow:
            async def is_open(self, wa_id):
                return wa_id == "447700900001"

        runtime.contact_window = FakeWindow()
        assert await runtime._window_open("447700900001") is True
        assert await runtime._window_open("447700900002") is False

    async def test_an_exception_fails_closed(self, monkeypatch):
        runtime = self._runtime(monkeypatch)

        class BrokenWindow:
            async def is_open(self, wa_id):
                raise RuntimeError("db down")

        runtime.contact_window = BrokenWindow()
        assert await runtime._window_open("447700900001") is False


class TestInboundIsRecordedBeforeRouting:
    """record_inbound must fire unconditionally, before any routing decision -
    the window opens on receipt regardless of what the message goes on to do,
    including messages that get ignored entirely."""

    def _runtime_and_settings(self, monkeypatch):
        for k, v in {
            "AZURE_OPENAI_ENDPOINT": "https://e.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "k", "LLM_MODEL": "azure_openai:m",
            "CONTRACTOR_WA_ID": "447700900555",
        }.items():
            monkeypatch.setenv(k, v)
        from winch.app import Runtime
        from winch.config import Settings
        settings = Settings.from_env()
        return Runtime(settings), settings

    async def test_status_events_do_not_record_inbound(self, monkeypatch):
        """Status callbacks (delivered/read) are not messages from a number
        and must not be treated as one opening a window."""
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)
        recorded = []

        class FakeWindow:
            async def record_inbound(self, wa_id):
                recorded.append(wa_id)

        runtime.contact_window = FakeWindow()
        event = ParsedEvent(kind="status", provider_message_id="wamid.s1",
                           status="delivered", timestamp=datetime.now(timezone.utc))
        await _handle(runtime, settings, event)
        assert recorded == []

    async def test_unknown_customer_message_still_records_inbound(self, monkeypatch):
        """Even a message from a number we do not recognise and end up
        ignoring must still open that number's window - the record happens
        before the 'unknown number' routing decision is made."""
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)
        recorded = []

        class FakeWindow:
            async def record_inbound(self, wa_id):
                recorded.append(wa_id)

        class FakeThreads:
            async def thread_for_customer(self, wa_id):
                return None

        runtime.contact_window = FakeWindow()
        runtime.threads = FakeThreads()
        event = ParsedEvent(kind="message", provider_message_id="wamid.u1",
                           from_wa_id="447700900099", text="hello",
                           timestamp=datetime.now(timezone.utc))
        await _handle(runtime, settings, event)
        assert recorded == ["447700900099"]

    async def test_a_failure_recording_inbound_does_not_block_processing(self, monkeypatch):
        """record_inbound is best-effort - a DB hiccup here must not stop the
        rest of the message from being processed."""
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)

        class BrokenWindow:
            async def record_inbound(self, wa_id):
                raise RuntimeError("db down")

        class FakeThreads:
            async def thread_for_customer(self, wa_id):
                return None

        runtime.contact_window = BrokenWindow()
        runtime.threads = FakeThreads()
        event = ParsedEvent(kind="message", provider_message_id="wamid.u2",
                           from_wa_id="447700900099", text="hello",
                           timestamp=datetime.now(timezone.utc))
        # must not raise
        await _handle(runtime, settings, event)


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


class TestLogging:
    """Application logs must actually be emitted.

    Python's root logger defaults to WARNING, so logger.info() calls in this
    package were silently discarded in production while uvicorn's own access
    logs appeared. Several real failures looked like silence because of it.
    """

    def test_configure_logging_enables_info_for_the_package(self):
        import logging

        from winch.app import configure_logging

        logging.getLogger("winch").setLevel(logging.WARNING)
        configure_logging("INFO")
        assert logging.getLogger("winch").isEnabledFor(logging.INFO)
        assert logging.getLogger("winch.webhook").isEnabledFor(logging.INFO)

    def test_a_module_logger_actually_emits(self, caplog):
        import logging

        from winch.app import configure_logging

        configure_logging("INFO")
        with caplog.at_level(logging.INFO, logger="winch.app"):
            logging.getLogger("winch.app").info("intake: quote=%s", "q1")
        assert any("intake: quote=q1" in r.getMessage() for r in caplog.records)

    def test_no_argument_path_works(self, monkeypatch):
        """configure_logging() with no argument reads os.environ.

        Every other test passed an explicit level, so this branch was never
        executed - and it shipped with `os` unimported, crashing the container
        on startup with a NameError.
        """
        import logging

        from winch.app import configure_logging

        monkeypatch.delenv("LOG_LEVEL", raising=False)
        configure_logging()
        assert logging.getLogger("winch").isEnabledFor(logging.INFO)

    def test_log_level_env_var_is_honoured(self, monkeypatch):
        import logging

        from winch.app import configure_logging

        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        configure_logging()
        assert not logging.getLogger("winch").isEnabledFor(logging.INFO)
        monkeypatch.delenv("LOG_LEVEL")
        configure_logging()

    def test_level_is_overridable(self):
        import logging

        from winch.app import configure_logging

        configure_logging("WARNING")
        assert not logging.getLogger("winch").isEnabledFor(logging.INFO)
        configure_logging("INFO")


class TestAppBoots:
    """A container-shaped smoke test.

    create_app() crashed on startup with a NameError because configure_logging
    used os.environ while `os` was unimported - and every unit test passed an
    explicit level, so that branch never ran. Building the whole app the way the
    container does catches this class of error before a deploy does.
    """

    def _env(self, monkeypatch):
        for k, v in {
            "AZURE_OPENAI_ENDPOINT": "https://e.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "k", "LLM_MODEL": "azure_openai:m",
        }.items():
            monkeypatch.setenv(k, v)

    def test_create_app_succeeds(self, monkeypatch):
        self._env(monkeypatch)
        from winch.app import create_app
        assert create_app() is not None

    def test_every_expected_route_is_served(self, monkeypatch):
        self._env(monkeypatch)
        from winch.app import create_app
        paths = set(create_app().openapi()["paths"])
        assert {"/webhook/meta", "/internal/tick", "/internal/events",
                "/health", "/privacy", "/terms", "/data-deletion"} <= paths


class TestInterruptNotification:
    """The bug that stopped every real message reaching the contractor.

    interrupt() correctly pauses the graph and returns a payload in
    result["__interrupt__"], but nothing in _handle ever read that value and
    sent it anywhere. Four real quotes parsed successfully end-to-end and the
    contractor received nothing, because the graph was genuinely waiting at
    collect_contact - there was just no message telling them so.
    """

    def test_collect_contact_payload_asks_for_missing_fields(self):
        from winch.app import _render_interrupt

        text = _render_interrupt({
            "kind": "collect_contact",
            "missing": ["customer_phone"],
            "draft": {"project_title": "2km stock fencing"},
        })
        assert "2km stock fencing" in text
        assert "mobile number" in text

    def test_confirm_quote_shows_the_frozen_total_and_asks_to_activate(self):
        from winch.app import _render_interrupt

        text = _render_interrupt({
            "kind": "confirm_quote",
            "draft": {"quote_total": 24504.0, "currency": "GBP",
                     "customer_name": "Mark Henderson", "customer_phone": "07700900412",
                     "project_title": "2km stock fencing"},
            "cadence_days": [2, 5, 9],
        })
        assert "24,504.00" in text
        assert "Mark Henderson" in text
        assert "YES" in text

    def test_confirm_quote_names_the_customer_for_disambiguation(self):
        """With several quotes in flight, the contractor must be able to tell
        which one a prompt is about just by reading it - the customer name is
        the cheapest identifier available."""
        from winch.app import _render_interrupt

        text = _render_interrupt({
            "kind": "confirm_quote",
            "draft": {"quote_total": 1000.0, "currency": "GBP",
                     "customer_name": "Denise Okafor", "customer_phone": "44700",
                     "project_title": "Shed"},
            "cadence_days": [2, 5, 9],
        })
        assert "Denise Okafor" in text

    def test_every_gate_nudges_a_swipe_reply(self):
        """The swipe-reply hint is what lets a bare 'yes' resolve to the exact
        quote it answers instead of guessing 'most recently awaiting' - which
        silently approved the wrong quote once two were in flight at once."""
        from winch.app import _render_interrupt

        for payload in (
            {"kind": "collect_contact", "missing": ["customer_phone"], "draft": {}},
            {"kind": "confirm_quote", "draft": {"customer_name": "X"}, "cadence_days": [2]},
            {"kind": "approve_send", "template": "t", "preview": "p"},
        ):
            assert "swipe" in _render_interrupt(payload).lower()

    def test_approve_send_shows_the_preview(self):
        from winch.app import _render_interrupt

        text = _render_interrupt({
            "kind": "approve_send", "template": "checkin_soft",
            "preview": "[checkin_soft] Mark | Dave | ...",
        })
        assert "checkin_soft" in text
        assert "HOLD" in text

    def test_unknown_kind_does_not_crash(self):
        from winch.app import _render_interrupt

        assert "mystery" in _render_interrupt({"kind": "mystery"})

    async def test_notify_sends_when_the_result_has_an_interrupt(self):
        from winch.app import _notify_contractor_of_interrupt
        from winch.protocols import SendResult

        class FakeInterrupt:
            value = {"kind": "collect_contact", "missing": ["customer_phone"], "draft": {}}

        sent = []

        class FakeChannel:
            async def send_freeform(self, to, body):
                sent.append((to, body))
                return SendResult(ok=True, provider_message_id="wamid.prompt1")

        class FakeThreads:
            recorded = []
            async def record_prompt(self, quote_id, message_id):
                self.recorded.append((quote_id, message_id))

        class FakeRuntime:
            threads = FakeThreads()

        await _notify_contractor_of_interrupt(
            FakeRuntime(), FakeChannel(), "44770", {"__interrupt__": [FakeInterrupt()]},
            "q_123",
        )
        assert len(sent) == 1
        assert sent[0][0] == "44770"

    async def test_notify_records_the_prompt_message_id_for_later_disambiguation(self):
        from winch.app import _notify_contractor_of_interrupt
        from winch.protocols import SendResult

        class FakeInterrupt:
            value = {"kind": "confirm_quote", "draft": {}, "cadence_days": []}

        class FakeChannel:
            async def send_freeform(self, to, body):
                return SendResult(ok=True, provider_message_id="wamid.prompt42")

        recorded = []

        class FakeThreads:
            async def record_prompt(self, quote_id, message_id):
                recorded.append((quote_id, message_id))

        class FakeRuntime:
            threads = FakeThreads()

        await _notify_contractor_of_interrupt(
            FakeRuntime(), FakeChannel(), "44770", {"__interrupt__": [FakeInterrupt()]},
            "q_abc",
        )
        assert recorded == [("q_abc", "wamid.prompt42")]

    async def test_a_failed_send_does_not_record_a_prompt(self):
        from winch.app import _notify_contractor_of_interrupt
        from winch.protocols import SendResult

        class FakeInterrupt:
            value = {"kind": "confirm_quote", "draft": {}, "cadence_days": []}

        class FakeChannel:
            async def send_freeform(self, to, body):
                return SendResult(ok=False)

        recorded = []

        class FakeThreads:
            async def record_prompt(self, quote_id, message_id):
                recorded.append((quote_id, message_id))

        class FakeRuntime:
            threads = FakeThreads()

        await _notify_contractor_of_interrupt(
            FakeRuntime(), FakeChannel(), "44770", {"__interrupt__": [FakeInterrupt()]},
            "q_abc",
        )
        assert recorded == []

    async def test_notify_sends_nothing_when_the_graph_ran_to_completion(self):
        """A result with no __interrupt__ key means the graph finished or ended
        - nothing to tell the contractor, and definitely not a spurious message."""
        from winch.app import _notify_contractor_of_interrupt

        sent = []

        class FakeChannel:
            async def send_freeform(self, to, body):
                sent.append((to, body))

        await _notify_contractor_of_interrupt(
            None, FakeChannel(), "44770", {"status": "CLOSED"}, "q_1"
        )
        assert sent == []


class TestPendingThreadDisambiguation:
    """The bug reported directly by a real pilot session: with two quotes
    simultaneously awaiting a contractor answer, replying 'yes' silently
    approved the WRONG one - the newest, not the one actually being looked at.
    _pending_thread must prefer an exact resolution from a swipe-reply over
    the "most recently awaiting" guess.
    """

    def _runtime(self, monkeypatch):
        for k, v in {
            "AZURE_OPENAI_ENDPOINT": "https://e.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "k", "LLM_MODEL": "azure_openai:m",
        }.items():
            monkeypatch.setenv(k, v)
        from winch.app import Runtime
        from winch.config import Settings
        return Runtime(Settings.from_env())

    async def test_swipe_reply_resolves_to_the_exact_quote_not_the_newest(self, monkeypatch):
        from winch.app import _pending_thread

        runtime = self._runtime(monkeypatch)

        class FakeThreads:
            async def resolve_reply(self, reply_to_message_id):
                # Simulates: this prompt id belongs to the OLDER quote, even
                # though a newer one is also awaiting.
                return "q_older" if reply_to_message_id == "wamid.prompt_old" else None

            async def pending_thread(self):
                return "q_newest"  # what the old buggy fallback would return

        runtime.threads = FakeThreads()
        result = await _pending_thread(runtime, "44770", "wamid.prompt_old")
        assert result == "q_older", "swipe-reply must win over 'most recently awaiting'"

    async def test_no_reply_context_falls_back_to_most_recently_awaiting(self, monkeypatch):
        from winch.app import _pending_thread

        runtime = self._runtime(monkeypatch)

        class FakeThreads:
            async def resolve_reply(self, reply_to_message_id):
                return None

            async def pending_thread(self):
                return "q_newest"

        runtime.threads = FakeThreads()
        result = await _pending_thread(runtime, "44770", None)
        assert result == "q_newest"

    async def test_reply_to_an_unrecorded_message_falls_back(self, monkeypatch):
        """A swipe-reply to something that isn't a recorded prompt (e.g. an
        old status message) must not crash - just fall back."""
        from winch.app import _pending_thread

        runtime = self._runtime(monkeypatch)

        class FakeThreads:
            async def resolve_reply(self, reply_to_message_id):
                return None

            async def pending_thread(self):
                return "q_fallback"

        runtime.threads = FakeThreads()
        result = await _pending_thread(runtime, "44770", "wamid.unrelated")
        assert result == "q_fallback"


class TestUnclearReplyRendering:
    """The re-ask prompts for the two ambiguous-reply gates."""

    def test_confirm_quote_unclear_names_the_customer_and_offers_yes_no(self):
        from winch.app import _render_interrupt

        text = _render_interrupt({
            "kind": "confirm_quote_unclear",
            "draft": {"customer_name": "Mark Henderson"},
            "heard": "check in now",
        })
        assert "Mark Henderson" in text
        assert "check in now" in text
        assert "YES" in text and "NO" in text

    def test_confirm_quote_unclear_degrades_gracefully_with_no_heard_text(self):
        from winch.app import _render_interrupt

        text = _render_interrupt({"kind": "confirm_quote_unclear", "draft": {}})
        assert "YES" in text

    def test_approve_send_unclear_names_the_template(self):
        from winch.app import _render_interrupt

        text = _render_interrupt({
            "kind": "approve_send_unclear", "template": "checkin_soft",
            "heard": "go on then",
        })
        assert "checkin_soft" in text
        assert "go on then" in text
        assert "YES" in text and "HOLD" in text


class TestErrorVisibilitySafetyNet:
    """A failure during message processing must never be pure silence to the
    contractor - it was previously swallowed one layer up (webhook.py's
    per-event catch) with nothing sent to WhatsApp, indistinguishable from the
    product simply not working. Every unhandled exception must now produce a
    plain apology message before the exception is re-raised (so webhook.py's
    dedup-release behaviour for Meta's redelivery is unaffected).
    """

    def _runtime_and_settings(self, monkeypatch):
        for k, v in {
            "AZURE_OPENAI_ENDPOINT": "https://e.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "k", "LLM_MODEL": "azure_openai:m",
            "CONTRACTOR_WA_ID": "447700900555",
        }.items():
            monkeypatch.setenv(k, v)
        from winch.app import Runtime
        from winch.config import Settings
        settings = Settings.from_env()
        return Runtime(settings), settings

    async def test_an_unhandled_exception_notifies_the_contractor(self, monkeypatch):
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)
        sent = []

        class FakeContractorChannel:
            async def send_freeform(self, to, body):
                sent.append((to, body))

        class FakeContactWindow:
            async def record_inbound(self, wa_id):
                pass

        class BrokenThreads:
            async def resolve_reply(self, reply_to_message_id):
                return None

            async def pending_thread(self):
                raise RuntimeError("db exploded")

        runtime.contractor_channel = FakeContractorChannel()
        runtime.contact_window = FakeContactWindow()
        runtime.threads = BrokenThreads()

        event = ParsedEvent(kind="message", provider_message_id="wamid.err1",
                           from_wa_id="447700900555", text="yes",
                           timestamp=datetime.now(timezone.utc))

        with pytest.raises(RuntimeError):
            await _handle(runtime, settings, event)

        assert len(sent) == 1
        assert sent[0][0] == "447700900555"
        assert "went wrong" in sent[0][1].lower()

    async def test_the_exception_is_still_re_raised_after_notifying(self, monkeypatch):
        """webhook.py's own handler depends on the exception propagating, so
        it can release the dedup lock for Meta's redelivery."""
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)

        class FakeContractorChannel:
            async def send_freeform(self, to, body):
                pass

        class FakeContactWindow:
            async def record_inbound(self, wa_id):
                pass

        class BrokenThreads:
            async def resolve_reply(self, reply_to_message_id):
                return None

            async def pending_thread(self):
                raise ValueError("specific failure")

        runtime.contractor_channel = FakeContractorChannel()
        runtime.contact_window = FakeContactWindow()
        runtime.threads = BrokenThreads()

        event = ParsedEvent(kind="message", provider_message_id="wamid.err2",
                           from_wa_id="447700900555", text="yes",
                           timestamp=datetime.now(timezone.utc))

        with pytest.raises(ValueError, match="specific failure"):
            await _handle(runtime, settings, event)

    async def test_a_second_failure_while_notifying_does_not_mask_the_first(self, monkeypatch):
        """If even the apology message fails to send, that must not swallow
        or replace the original exception - the real error still propagates
        and gets logged."""
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)

        class DoublyBrokenChannel:
            async def send_freeform(self, to, body):
                raise ConnectionError("also broken")

        class FakeContactWindow:
            async def record_inbound(self, wa_id):
                pass

        class BrokenThreads:
            async def resolve_reply(self, reply_to_message_id):
                return None

            async def pending_thread(self):
                raise RuntimeError("the real error")

        runtime.contractor_channel = DoublyBrokenChannel()
        runtime.contact_window = FakeContactWindow()
        runtime.threads = BrokenThreads()

        event = ParsedEvent(kind="message", provider_message_id="wamid.err3",
                           from_wa_id="447700900555", text="yes",
                           timestamp=datetime.now(timezone.utc))

        with pytest.raises(RuntimeError, match="the real error"):
            await _handle(runtime, settings, event)

    async def test_success_path_sends_no_apology(self, monkeypatch):
        """The safety net specifically (an apology for an unhandled exception)
        must not fire on the happy path - a normal 'nothing pending'
        acknowledgment is not an apology and is expected. See
        TestContractorMessageWithNothingPending for that behaviour."""
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)
        sent = []

        class FakeContractorChannel:
            async def send_freeform(self, to, body):
                sent.append((to, body))

        class FakeContactWindow:
            async def record_inbound(self, wa_id):
                pass

        class FakeThreads:
            async def resolve_reply(self, reply_to_message_id):
                return None

            async def pending_thread(self):
                return None  # nothing pending - a normal outcome

        runtime.contractor_channel = FakeContractorChannel()
        runtime.contact_window = FakeContactWindow()
        runtime.threads = FakeThreads()

        event = ParsedEvent(kind="message", provider_message_id="wamid.ok1",
                           from_wa_id="447700900555", text="random chatter",
                           timestamp=datetime.now(timezone.utc))
        await _handle(runtime, settings, event)
        assert not any("went wrong" in body.lower() for _, body in sent)


class TestContractorMessageWithNothingPending:
    """A contractor message ('are you there?', 'did you do it?') that does
    not correspond to any pending interrupt was previously silently ignored -
    no error, no acknowledgment, nothing. Observed directly: two real
    messages sent in a row got zero response, indistinguishable from the
    product being broken. Reserve true silence for cases with nothing useful
    to say; a direct message always deserves an answer.
    """

    def _runtime_and_settings(self, monkeypatch):
        for k, v in {
            "AZURE_OPENAI_ENDPOINT": "https://e.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "k", "LLM_MODEL": "azure_openai:m",
            "CONTRACTOR_WA_ID": "447700900555",
        }.items():
            monkeypatch.setenv(k, v)
        from winch.app import Runtime
        from winch.config import Settings
        settings = Settings.from_env()
        return Runtime(settings), settings

    async def test_a_message_with_nothing_pending_gets_acknowledged(self, monkeypatch):
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)
        sent = []

        class FakeContractorChannel:
            async def send_freeform(self, to, body):
                sent.append((to, body))

        class FakeContactWindow:
            async def record_inbound(self, wa_id):
                pass

        class FakeThreads:
            async def resolve_reply(self, reply_to_message_id):
                return None

            async def pending_thread(self):
                return None

        runtime.contractor_channel = FakeContractorChannel()
        runtime.contact_window = FakeContactWindow()
        runtime.threads = FakeThreads()

        event = ParsedEvent(kind="message", provider_message_id="wamid.chat1",
                           from_wa_id="447700900555", text="are you there?",
                           timestamp=datetime.now(timezone.utc))
        await _handle(runtime, settings, event)

        assert len(sent) == 1
        assert sent[0][0] == "447700900555"
        assert "nothing outstanding" in sent[0][1].lower()


class TestCrashedIntakeDoesNotPoisonFutureMessages:
    """A real production incident: intake crashed (media download failed on
    an expired token) before the graph ever reached an interrupt. The thread
    had already been marked 'awaiting' before the graph ran, so it stayed
    awaiting forever with nothing genuinely pending. The contractor's very
    next, completely unrelated plain-text message ('hello there') was then
    hijacked by pending_thread()'s 'most recently awaiting' fallback into
    retrying that dead, crashed quote - and failed the exact same way,
    instead of getting the 'nothing outstanding' acknowledgment it should
    have.
    """

    def _runtime_and_settings(self, monkeypatch):
        for k, v in {
            "AZURE_OPENAI_ENDPOINT": "https://e.openai.azure.com",
            "AZURE_OPENAI_API_KEY": "k", "LLM_MODEL": "azure_openai:m",
            "CONTRACTOR_WA_ID": "447700900555",
        }.items():
            monkeypatch.setenv(k, v)
        from winch.app import Runtime
        from winch.config import Settings
        settings = Settings.from_env()
        return Runtime(settings), settings

    async def test_a_crashed_intake_never_marks_the_thread_awaiting(self, monkeypatch):
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)
        marked = []

        class FakeGraph:
            async def ainvoke(self, state, config):
                raise RuntimeError("media download failed: token expired")

        class FakeThreads:
            async def mark_awaiting(self, quote_id, awaiting):
                marked.append((quote_id, awaiting))

        class FakeContractorChannel:
            async def send_freeform(self, to, body):
                pass

        class FakeContactWindow:
            async def record_inbound(self, wa_id):
                pass

        runtime.graph = FakeGraph()
        runtime.threads = FakeThreads()
        runtime.contractor_channel = FakeContractorChannel()
        runtime.contact_window = FakeContactWindow()

        event = ParsedEvent(kind="message", provider_message_id="wamid.crash1",
                           from_wa_id="447700900555", media_id="m1",
                           media_mime="application/pdf",
                           timestamp=datetime.now(timezone.utc))

        with pytest.raises(RuntimeError):
            await _handle(runtime, settings, event)

        assert marked == [], (
            "a crashed intake must never mark its thread awaiting - "
            "otherwise it silently hijacks the contractor's next message"
        )

    async def test_a_successful_intake_that_interrupts_does_mark_awaiting(self, monkeypatch):
        """Regression guard the other way: the normal, working case must
        still mark the thread awaiting so a real reply can resume it."""
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)
        marked = []

        class FakeInterrupt:
            value = {"kind": "collect_contact", "missing": ["customer_phone"], "draft": {}}

        class FakeGraph:
            async def ainvoke(self, state, config):
                return {"__interrupt__": [FakeInterrupt()]}

        class FakeThreads:
            async def mark_awaiting(self, quote_id, awaiting):
                marked.append((quote_id, awaiting))

            async def record_prompt(self, quote_id, message_id):
                pass

        class FakeContractorChannel:
            async def send_freeform(self, to, body):
                from winch.protocols import SendResult
                return SendResult(ok=True, provider_message_id="wamid.prompt1")

        class FakeContactWindow:
            async def record_inbound(self, wa_id):
                pass

        runtime.graph = FakeGraph()
        runtime.threads = FakeThreads()
        runtime.contractor_channel = FakeContractorChannel()
        runtime.contact_window = FakeContactWindow()

        event = ParsedEvent(kind="message", provider_message_id="wamid.ok2",
                           from_wa_id="447700900555", media_id="m2",
                           media_mime="application/pdf",
                           timestamp=datetime.now(timezone.utc))
        await _handle(runtime, settings, event)

        assert len(marked) == 1
        assert marked[0][1] is True

    async def test_after_a_crash_a_new_unrelated_message_gets_acknowledged_not_retried(self, monkeypatch):
        """The end-to-end regression: crash, then a plain message, must reach
        the 'nothing pending' acknowledgment - not resume the dead thread."""
        from winch.app import _handle
        from winch.webhook import ParsedEvent
        from datetime import datetime, timezone

        runtime, settings = self._runtime_and_settings(monkeypatch)
        sent = []
        thread_store = {"awaiting": None, "closed": set()}

        class FakeGraph:
            async def ainvoke(self, state, config):
                raise RuntimeError("media download failed: token expired")

        class FakeThreads:
            async def mark_awaiting(self, quote_id, awaiting):
                if awaiting:
                    thread_store["awaiting"] = quote_id
                elif thread_store["awaiting"] == quote_id:
                    thread_store["awaiting"] = None

            async def resolve_reply(self, reply_to_message_id):
                return None

            async def pending_thread(self):
                return thread_store["awaiting"]

        class FakeContractorChannel:
            async def send_freeform(self, to, body):
                sent.append(body)
                from winch.protocols import SendResult
                return SendResult(ok=True, provider_message_id="wamid.x")

        class FakeContactWindow:
            async def record_inbound(self, wa_id):
                pass

        runtime.graph = FakeGraph()
        runtime.threads = FakeThreads()
        runtime.contractor_channel = FakeContractorChannel()
        runtime.contact_window = FakeContactWindow()

        crash_event = ParsedEvent(kind="message", provider_message_id="wamid.crash2",
                                  from_wa_id="447700900555", media_id="m3",
                                  media_mime="application/pdf",
                                  timestamp=datetime.now(timezone.utc))
        with pytest.raises(RuntimeError):
            await _handle(runtime, settings, crash_event)

        sent.clear()  # discard the "something went wrong" apology from the crash

        followup = ParsedEvent(kind="message", provider_message_id="wamid.followup",
                               from_wa_id="447700900555", text="hello there",
                               timestamp=datetime.now(timezone.utc))
        await _handle(runtime, settings, followup)

        assert len(sent) == 1
        assert "nothing outstanding" in sent[0].lower(), (
            f"expected the nothing-pending acknowledgment, got: {sent[0]!r}"
        )
