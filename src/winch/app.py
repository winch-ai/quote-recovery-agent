"""FastAPI surface: the Meta webhook, the scheduler tick, and health.

The webhook translates Meta events into graph invocations; the tick drains the
durable touchpoint queue. Neither contains business logic — routing lives in
winch.supervisor and the flow lives in winch.graph.
"""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, FastAPI, Header, Response
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from langgraph.types import Command

from winch.channels.whatsapp import WhatsAppChannel
from winch.compose import ContractorProfile
from winch.config import Settings
from winch.db import init_schema, make_pool
from winch.graph import Entry, build_graph
from winch.llm.azure import AzureExtractor, AzureTextClient
from winch.nodes import Deps, new_quote_id
from winch.repository import (
    PostgresDeduplicator,
    PostgresEventSink,
    PostgresThreadIndex,
    PostgresTouchpointQueue,
)
from winch.state import InboundMessage, QuoteStatus
from winch.webhook import ParsedEvent, build_router

logger = logging.getLogger(__name__)


class Runtime:
    """Everything built once at boot and reused per request."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool = None
        self.cp_pool = None
        self.graph = None
        self.deduplicator = None
        self.queue = None
        self.events = None
        self.threads = None

    async def start(self) -> None:
        s = self.settings
        self.pool = await make_pool(s.database_url)
        await init_schema(self.pool)

        self.queue = PostgresTouchpointQueue(self.pool)
        self.events = PostgresEventSink(self.pool)
        self.deduplicator = PostgresDeduplicator(self.pool)
        self.threads = PostgresThreadIndex(self.pool)

        channel = WhatsAppChannel(
            phone_number_id=s.meta_phone_number_id,
            access_token=s.meta_access_token,
            graph_version=s.meta_graph_version,
            window_checker=self._window_open,
        )
        extractor = AzureExtractor(s.azure_endpoint, s.azure_api_key,
                                   s.azure_deployment, s.azure_api_version)
        text = AzureTextClient(s.azure_endpoint, s.azure_api_key,
                               s.azure_deployment, s.azure_api_version)

        deps = Deps(
            llm=_CombinedLLM(extractor, text),
            channel=channel,
            contractor_channel=channel,
            events=self.events,
            queue=self.queue,
            contractor=ContractorProfile(
                contractor_id="pilot",
                first_name=s.contractor_first_name,
                business_name=s.contractor_business_name,
                wa_id=s.contractor_wa_id,
                timezone=s.contractor_timezone,
            ),
            media_fetch=channel.download_media,
        )

        # The checkpointer's setup() runs CREATE INDEX CONCURRENTLY, which
        # Postgres refuses inside a transaction block. psycopg pools are
        # transactional by default, so the checkpointer gets its own autocommit
        # pool rather than making every app query autocommit.
        self.cp_pool = AsyncConnectionPool(
            s.database_url, min_size=1, max_size=4, open=False,
            kwargs={"autocommit": True},
        )
        await self.cp_pool.open()
        await self.cp_pool.wait()

        checkpointer = AsyncPostgresSaver(self.cp_pool)
        await checkpointer.setup()
        self.graph = build_graph(deps, checkpointer=checkpointer)

    async def _window_open(self, to: str) -> bool:
        """A 24-hour window is open only if that number messaged us recently.

        Fails closed: any error means 'not open', so a failure can never cause
        an out-of-window free-form send.
        """
        try:
            async with self.pool.connection() as conn:
                cur = await conn.execute(
                    """SELECT 1 FROM events
                       WHERE event_type = 'customer_replied'
                         AND payload->>'from' = %s
                         AND at > now() - interval '24 hours' LIMIT 1""",
                    (to,),
                )
                return await cur.fetchone() is not None
        except Exception:
            logger.exception("window check failed for a recipient; failing closed")
            return False

    async def stop(self) -> None:
        for pool in (self.cp_pool, self.pool):
            if pool is not None:
                await pool.close()


class _CombinedLLM:
    """Satisfies LLMClient by delegating to the extraction and text clients."""

    def __init__(self, extractor, text):
        self._extractor, self._text = extractor, text

    async def extract_quote(self, media: bytes, mime_type: str):
        return await self._extractor.extract_quote(media, mime_type)

    async def classify_intent(self, text: str):
        return await self._text.classify_intent(text)

    async def compose_reply(self, quote, customer_message: str) -> str:
        return await self._text.compose_reply(quote, customer_message)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="Winch", lifespan=lifespan)

    async def on_event(event: ParsedEvent) -> None:
        await _handle(runtime, settings, event)

    app.include_router(build_router(
        app_secret=settings.meta_app_secret,
        verify_token=settings.meta_verify_token,
        deduplicator=runtime.deduplicator,
        on_event=on_event,
    ))
    app.include_router(_internal_router(runtime, settings))
    return app


async def _handle(runtime: Runtime, settings: Settings, event: ParsedEvent) -> None:
    """Translate one Meta event into a graph invocation."""
    if event.kind == "status":
        return

    from_contractor = event.from_wa_id == settings.contractor_wa_id

    if from_contractor and event.media_id:
        quote_id = new_quote_id()
        await runtime.threads.mark_awaiting(quote_id, True)
        await runtime.graph.ainvoke(
            {"quote_id": quote_id, "media_id": event.media_id,
             "status": QuoteStatus.DRAFT, "_entry": Entry.INTAKE},
            {"configurable": {"thread_id": quote_id}},
        )
        return

    if from_contractor:
        # A reply from the contractor resumes whichever interrupt is pending.
        thread = await _pending_thread(runtime, event.from_wa_id)
        if thread is None:
            logger.info("contractor message with no pending interrupt; ignoring")
            return
        await runtime.graph.ainvoke(
            Command(resume=_parse_contractor_reply(event)),
            {"configurable": {"thread_id": thread}},
        )
        await _sync_thread_index(runtime, thread)
        return

    thread = await _thread_for_customer(runtime, event.from_wa_id)
    if thread is None:
        logger.info("inbound from an unknown number; ignoring")
        return
    inbound = InboundMessage(
        provider_message_id=event.provider_message_id, from_customer=True,
        text=event.text or event.button_payload or "",
        received_at=event.timestamp,
    )
    await runtime.graph.ainvoke(
        {"_entry": Entry.INBOUND, "inbound": [inbound]},
        {"configurable": {"thread_id": thread}},
    )


async def _sync_thread_index(runtime: Runtime, thread: str) -> None:
    """Keep the index in step with the graph after a resume.

    Binds the customer's number once a quote is approved (that is when we first
    have it), clears the awaiting flag when nothing is parked, and closes the
    thread when the quote is finished.
    """
    snapshot = await runtime.graph.aget_state({"configurable": {"thread_id": thread}})
    values = snapshot.values or {}
    quote = values.get("quote")
    if quote is not None and quote.customer_phone:
        await runtime.threads.bind_customer(thread, quote.customer_phone)
    if values.get("status") == QuoteStatus.CLOSED:
        await runtime.threads.close(thread)
        return
    await runtime.threads.mark_awaiting(thread, bool(snapshot.next))


def _parse_contractor_reply(event: ParsedEvent) -> dict:
    """Map a button tap or a plain reply onto a resume payload."""
    payload = (event.button_payload or event.text or "").strip().lower()
    if payload in {"yes", "approve", "send", "ok", "start", "1"}:
        return {"approved": True}
    if payload in {"no", "hold", "stop", "cancel", "0"}:
        return {"approved": False}
    return {"text": event.text or ""}


async def _pending_thread(runtime: Runtime, wa_id: str) -> str | None:
    """Which thread is waiting on a contractor answer. v1 serves one contractor,
    so `wa_id` is not yet a discriminator - it is taken for the signature the
    multi-tenant version will need."""
    return await runtime.threads.pending_thread()


async def _thread_for_customer(runtime: Runtime, wa_id: str) -> str | None:
    return await runtime.threads.thread_for_customer(wa_id)


def _internal_router(runtime: Runtime, settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict:
        """Not /healthz: Google's frontend intercepts that path on Cloud Run and
        returns its own 404, so the request never reaches the container."""
        return {"ok": True}

    @router.post("/internal/tick")
    async def tick(
        response: Response,
        x_tick_secret: str = Header(default=""),
    ) -> dict:
        """Drain the durable queue. Cloud Scheduler calls this every 5 minutes.

        claim_due is atomic under concurrency, so several instances ticking at
        once cannot claim the same touchpoint.

        Fails closed: an unset TICK_SECRET rejects everything rather than
        accepting everything, so a misconfigured deploy is inert instead of
        wide open.
        """
        if not settings.tick_secret or not hmac.compare_digest(
            x_tick_secret, settings.tick_secret
        ):
            logger.warning("tick rejected: bad or missing X-Tick-Secret")
            response.status_code = 403
            return {"error": "forbidden"}

        now = datetime.now(timezone.utc)
        claimed = await runtime.queue.claim_due(now)
        for due in claimed:
            try:
                await runtime.graph.ainvoke(
                    {"_entry": Entry.TICK, "pending_gate_index": due.touchpoint_index},
                    {"configurable": {"thread_id": due.quote_id}},
                )
            except Exception:
                logger.exception("tick failed for %s/%s", due.quote_id,
                                 due.touchpoint_index)
        return {"claimed": len(claimed)}

    return router
