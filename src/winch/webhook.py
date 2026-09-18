"""Meta WhatsApp Cloud API webhook handling: verification, parsing, and deduplication.

CONTRACT FILE implementation for meta-webhook.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict

from winch.protocols import MessageDeduplicator

logger = logging.getLogger(__name__)


class InMemoryDeduplicator:
    """Satisfies MessageDeduplicator. Process-local; Postgres impl comes later."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self.max_entries = max_entries
        self._seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._lock = asyncio.Lock()

    async def release(self, provider_message_id: str) -> None:
        """Un-claim a failed id so Meta's redelivery gets another attempt."""
        async with self._lock:
            self._seen.pop(provider_message_id, None)

    async def seen(self, provider_message_id: str) -> bool:
        """Claim the id. Return True if it had already been claimed."""
        async with self._lock:
            if provider_message_id in self._seen:
                return True
            self._seen[provider_message_id] = None
            while len(self._seen) > self.max_entries:
                self._seen.popitem(last=False)
            return False


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 header.

    Header format is 'sha256=<hex>'. Returns False for None, empty, malformed,
    wrong-prefix, or mismatched. MUST use hmac.compare_digest. MUST NOT raise.
    """
    try:
        if not isinstance(app_secret, str) or not isinstance(raw_body, (bytes, bytearray)):
            return False
        if not header or not isinstance(header, str) or not header.startswith("sha256="):
            return False

        signature = header[7:]
        if not signature:
            return False

        expected = hmac.new(
            app_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature.lower(), expected)
    except Exception as exc:
        logger.warning("verify_signature failed with exception: %s", type(exc).__name__)
        return False


class ParsedEvent(BaseModel):
    """One normalised inbound item. A single POST may contain several."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["message", "status"]
    provider_message_id: str
    from_wa_id: str | None = None
    text: str | None = None
    media_id: str | None = None
    media_mime: str | None = None
    button_payload: str | None = None
    status: str | None = None  # sent|delivered|read|failed
    error_code: int | None = None  # e.g. 131026
    timestamp: datetime


def _parse_timestamp(raw_ts: Any) -> datetime:
    """Convert raw timestamp (seconds or ISO) into UTC datetime."""
    if isinstance(raw_ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw_ts), tz=timezone.utc)
        except Exception as exc:
            logger.warning("Failed to parse numeric timestamp: %s", type(exc).__name__)
            return datetime.now(tz=timezone.utc)
    if isinstance(raw_ts, str):
        try:
            return datetime.fromtimestamp(float(raw_ts), tz=timezone.utc)
        except ValueError:
            try:
                return datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except Exception as exc:
                logger.warning("Failed to parse ISO timestamp string: %s", type(exc).__name__)
                return datetime.now(tz=timezone.utc)
    if isinstance(raw_ts, datetime):
        if raw_ts.tzinfo is None:
            return raw_ts.replace(tzinfo=timezone.utc)
        return raw_ts
    return datetime.now(tz=timezone.utc)


def _parse_message(message: dict[str, Any]) -> ParsedEvent | None:
    """Parse a single WhatsApp inbound message object."""
    msg_id = message.get("id")
    if not msg_id or not isinstance(msg_id, str):
        return None

    from_wa_id = message.get("from")
    if from_wa_id is not None and not isinstance(from_wa_id, str):
        from_wa_id = str(from_wa_id)

    ts = _parse_timestamp(message.get("timestamp"))
    msg_type = message.get("type")

    try:
        if msg_type == "text":
            text_obj = message.get("text")
            text = text_obj.get("body") if isinstance(text_obj, dict) else None
            return ParsedEvent(
                kind="message",
                provider_message_id=msg_id,
                from_wa_id=from_wa_id,
                text=text,
                timestamp=ts,
            )

        if msg_type == "document":
            doc = message.get("document")
            if not isinstance(doc, dict):
                return None
            media_id = str(doc["id"]) if doc.get("id") is not None else None
            media_mime = str(doc["mime_type"]) if doc.get("mime_type") is not None else None
            caption = str(doc["caption"]) if doc.get("caption") is not None else None
            return ParsedEvent(
                kind="message",
                provider_message_id=msg_id,
                from_wa_id=from_wa_id,
                media_id=media_id,
                media_mime=media_mime,
                text=caption,
                timestamp=ts,
            )

        if msg_type == "image":
            img = message.get("image")
            if not isinstance(img, dict):
                return None
            media_id = str(img["id"]) if img.get("id") is not None else None
            media_mime = str(img["mime_type"]) if img.get("mime_type") is not None else None
            caption = str(img["caption"]) if img.get("caption") is not None else None
            return ParsedEvent(
                kind="message",
                provider_message_id=msg_id,
                from_wa_id=from_wa_id,
                media_id=media_id,
                media_mime=media_mime,
                text=caption,
                timestamp=ts,
            )

        if msg_type == "interactive":
            interactive = message.get("interactive")
            if not isinstance(interactive, dict):
                return None
            itype = interactive.get("type")
            if itype == "button_reply" or (itype is None and "button_reply" in interactive):
                btn = interactive.get("button_reply")
                if not isinstance(btn, dict):
                    return None
                button_payload = (
                    str(btn["id"])
                    if btn.get("id") is not None
                    else (str(btn["payload"]) if btn.get("payload") is not None else None)
                )
                text = (
                    str(btn["title"])
                    if btn.get("title") is not None
                    else (str(btn["text"]) if btn.get("text") is not None else None)
                )
                return ParsedEvent(
                    kind="message",
                    provider_message_id=msg_id,
                    from_wa_id=from_wa_id,
                    button_payload=button_payload,
                    text=text,
                    timestamp=ts,
                )
            return None

        if msg_type == "button":
            btn = message.get("button")
            if not isinstance(btn, dict):
                return None
            button_payload = (
                str(btn["payload"])
                if btn.get("payload") is not None
                else (str(btn["id"]) if btn.get("id") is not None else None)
            )
            text = (
                str(btn["text"])
                if btn.get("text") is not None
                else (str(btn["title"]) if btn.get("title") is not None else None)
            )
            return ParsedEvent(
                kind="message",
                provider_message_id=msg_id,
                from_wa_id=from_wa_id,
                button_payload=button_payload,
                text=text,
                timestamp=ts,
            )

        # Unknown or unsupported message types (location, audio, video, sticker, etc.) are skipped
        return None
    except Exception as exc:
        logger.warning("Failed to construct ParsedEvent for message %s: %s", msg_id, type(exc).__name__)
        return None


def _parse_status(status_item: dict[str, Any]) -> ParsedEvent | None:
    """Parse a single WhatsApp status callback object."""
    status_id = status_item.get("id")
    if not status_id or not isinstance(status_id, str):
        return None

    status_val = status_item.get("status")
    if status_val is not None and not isinstance(status_val, str):
        status_val = str(status_val)

    from_wa_id = status_item.get("recipient_id") or status_item.get("from")
    if from_wa_id is not None and not isinstance(from_wa_id, str):
        from_wa_id = str(from_wa_id)

    ts = _parse_timestamp(status_item.get("timestamp"))

    error_code = None
    errors = status_item.get("errors")
    if isinstance(errors, list) and errors:
        first_err = errors[0]
        if isinstance(first_err, dict) and "code" in first_err:
            try:
                error_code = int(first_err["code"])
            except (ValueError, TypeError) as exc:
                logger.warning("Error code conversion failed: %s", type(exc).__name__)
    elif "error_code" in status_item:
        try:
            error_code = int(status_item["error_code"])
        except (ValueError, TypeError) as exc:
            logger.warning("Error code conversion failed: %s", type(exc).__name__)

    try:
        return ParsedEvent(
            kind="status",
            provider_message_id=status_id,
            from_wa_id=from_wa_id,
            status=status_val,
            error_code=error_code,
            timestamp=ts,
        )
    except Exception as exc:
        logger.warning("Failed to construct ParsedEvent for status %s: %s", status_id, type(exc).__name__)
        return None


def parse_webhook(payload: dict) -> list[ParsedEvent]:
    """Flatten Meta's entry[].changes[].value[] envelope.

    Handles: text messages, document messages, image messages, interactive
    button_reply, and status callbacks. Unknown types are SKIPPED, never raised
    on — Meta adds new ones without notice and a 500 causes redelivery storms.
    """
    events: list[ParsedEvent] = []
    if not isinstance(payload, dict):
        return events

    entries = payload.get("entry")
    if not isinstance(entries, list):
        return events

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue

            messages = value.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict):
                        event = _parse_message(message)
                        if event is not None:
                            events.append(event)

            statuses = value.get("statuses")
            if isinstance(statuses, list):
                for status_item in statuses:
                    if isinstance(status_item, dict):
                        event = _parse_status(status_item)
                        if event is not None:
                            events.append(event)

    return events


def build_router(
    app_secret: str,
    verify_token: str,
    deduplicator: MessageDeduplicator,
    on_event: Callable[[ParsedEvent], Awaitable[None]],
) -> APIRouter:
    """Router with:
       GET  /webhook/meta  -> hub.challenge echo when hub.verify_token matches,
                              403 otherwise
       POST /webhook/meta  -> 403 on bad signature; otherwise parse, drop
                              duplicates, await on_event for each new event,
                              return 200
    """
    router = APIRouter()

    @router.get("/webhook/meta")
    async def verify_webhook(request: Request) -> Response:
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")

        if token is not None and hmac.compare_digest(token, verify_token) and challenge is not None:
            return Response(content=str(challenge), media_type="text/plain", status_code=200)

        logger.warning("Meta webhook verification failed: token mismatch or missing challenge")
        return Response(content="Forbidden", status_code=403)

    @router.post("/webhook/meta")
    async def receive_webhook(request: Request) -> Response:
        raw_body = await request.body()
        signature_header = request.headers.get("x-hub-signature-256")

        if not verify_signature(app_secret, raw_body, signature_header):
            logger.warning("Meta webhook rejected: invalid signature")
            return Response(content="Forbidden", status_code=403)

        try:
            payload = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
        except Exception as exc:
            logger.warning("Failed to decode webhook JSON: %s", type(exc).__name__)
            return Response(content="OK", status_code=200)

        if not isinstance(payload, dict):
            logger.warning("Webhook payload root is not a dictionary")
            return Response(content="OK", status_code=200)

        events = parse_webhook(payload)
        # Structural diagnostic: message types and field names only, never
        # bodies, numbers or media content. Enough to tell a parser miss from a
        # routing miss, which guesswork could not.
        try:
            shapes = []
            for entry in payload.get("entry", []) or []:
                for change in entry.get("changes", []) or []:
                    value = change.get("value", {}) or {}
                    for m in value.get("messages", []) or []:
                        shapes.append({"type": m.get("type"), "keys": sorted(m.keys())})
                    for st in value.get("statuses", []) or []:
                        shapes.append({"status": st.get("status")})
            logger.info("webhook shape: field=%s items=%s parsed=%d",
                        (payload.get("entry", [{}]) or [{}])[0].get("changes", [{}])[0].get("field")
                        if payload.get("entry") else None,
                        shapes, len(events))
        except Exception:
            logger.exception("shape diagnostic failed (non-fatal)")

        for event in events:
            try:
                already_seen = await deduplicator.seen(event.provider_message_id)
            except Exception as exc:
                logger.error(
                    "Deduplicator error for message %s: %s",
                    event.provider_message_id,
                    type(exc).__name__,
                )
                continue

            if already_seen:
                continue

            try:
                await on_event(event)
            except Exception as exc:
                logger.exception(
                    "on_event failed for message %s: %s",
                    event.provider_message_id,
                    type(exc).__name__,
                )
                # Un-claim so Meta's redelivery retries rather than being
                # dropped as a duplicate. Losing a forwarded quote is worse
                # than handling one twice.
                try:
                    await deduplicator.release(event.provider_message_id)
                except Exception:
                    logger.exception(
                        "failed to release %s; event will be lost on redelivery",
                        event.provider_message_id,
                    )

        return Response(content="OK", status_code=200)

    return router
