"""Tests for Meta WhatsApp Cloud API webhook handling."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from winch.protocols import MessageDeduplicator
from winch.webhook import (
    InMemoryDeduplicator,
    ParsedEvent,
    build_router,
    parse_webhook,
    verify_signature,
)

APP_SECRET = "super_secret_app_key_12345"
VERIFY_TOKEN = "verify_token_abc_xyz"


def compute_sig(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# --- 1. Signature Verification Tests ---


def test_verify_signature_valid_and_invalid():
    body = b'{"object": "whatsapp_business_account"}'
    valid_sig = compute_sig(APP_SECRET, body)

    # Valid signature accepted
    assert verify_signature(APP_SECRET, body, valid_sig) is True

    # Wrong signature
    wrong_sig = "sha256=" + "a" * 64
    assert verify_signature(APP_SECRET, body, wrong_sig) is False

    # Missing header
    assert verify_signature(APP_SECRET, body, None) is False

    # Malformed headers
    assert verify_signature(APP_SECRET, body, "") is False
    assert verify_signature(APP_SECRET, body, "garbage") is False
    assert verify_signature(APP_SECRET, body, "sha256=") is False
    assert verify_signature(APP_SECRET, body, "sha1=" + "a" * 40) is False
    assert verify_signature(APP_SECRET, body, "sha256") is False

    # Empty body with valid signature
    empty_body = b""
    valid_empty_sig = compute_sig(APP_SECRET, empty_body)
    assert verify_signature(APP_SECRET, empty_body, valid_empty_sig) is True

    # Bad secret / body types do not raise
    assert verify_signature(12345, body, valid_sig) is False  # type: ignore[arg-type]
    assert verify_signature(APP_SECRET, "not bytes", valid_sig) is False  # type: ignore[arg-type]


def test_verify_signature_uses_compare_digest(monkeypatch: pytest.MonkeyPatch):
    called = False
    real_compare = hmac.compare_digest

    def fake_compare(a: Any, b: Any) -> bool:
        nonlocal called
        called = True
        return real_compare(a, b)

    monkeypatch.setattr(hmac, "compare_digest", fake_compare)
    body = b"test payload"
    sig = compute_sig(APP_SECRET, body)
    assert verify_signature(APP_SECRET, body, sig) is True
    assert called is True


# --- 2. InMemoryDeduplicator Tests ---


@pytest.mark.asyncio
async def test_in_memory_deduplicator_protocol_and_behavior():
    dedup = InMemoryDeduplicator(max_entries=3)
    assert isinstance(dedup, MessageDeduplicator)

    # First time seeing an ID -> returns False (recorded)
    assert await dedup.seen("wamid.1") is False

    # Second time seeing an ID -> returns True (already seen)
    assert await dedup.seen("wamid.1") is True

    # Different ID
    assert await dedup.seen("wamid.2") is False
    assert await dedup.seen("wamid.2") is True

    # Fill up to max_entries=3
    assert await dedup.seen("wamid.3") is False

    # Adding a 4th entry should evict the oldest ("wamid.1")
    assert await dedup.seen("wamid.4") is False

    # "wamid.1" was evicted, so seen should record it again and return False
    assert await dedup.seen("wamid.1") is False

    # "wamid.3" and "wamid.4" are still present
    assert await dedup.seen("wamid.3") is True
    assert await dedup.seen("wamid.4") is True


@pytest.mark.asyncio
async def test_in_memory_deduplicator_concurrency():
    dedup = InMemoryDeduplicator(max_entries=100)

    # Launch 50 concurrent seen() calls for the same ID
    results = await asyncio.gather(*[dedup.seen("concurrent_msg") for _ in range(50)])

    # Exactly one call should return False (the one that recorded it), the rest True
    assert results.count(False) == 1
    assert results.count(True) == 49


# --- 3. GET Verification Tests ---


@pytest.mark.asyncio
async def test_get_webhook_verification():
    dedup = InMemoryDeduplicator()
    mock_on_event = MagicMock()
    router = build_router(APP_SECRET, VERIFY_TOKEN, dedup, mock_on_event)
    app = FastAPI()
    app.include_router(router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Correct token echoes hub.challenge
        res = await client.get(
            "/webhook/meta",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )
        assert res.status_code == 200
        assert res.text == "1158201444"
        assert res.headers["content-type"].startswith("text/plain")

        # Wrong token yields 403
        res = await client.get(
            "/webhook/meta",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "1158201444",
            },
        )
        assert res.status_code == 403

        # Missing token yields 403
        res = await client.get(
            "/webhook/meta",
            params={"hub.mode": "subscribe", "hub.challenge": "1158201444"},
        )
        assert res.status_code == 403

        # Missing challenge yields 403
        res = await client.get(
            "/webhook/meta",
            params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN},
        )
        assert res.status_code == 403


# --- 4. POST Signature Validation Tests ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header_value",
    [
        None,
        "",
        "garbage",
        "sha256=",
        "sha256=" + "f" * 64,
        "sha1=abcdef",
    ],
)
async def test_post_bad_signatures_return_403(header_value: str | None):
    dedup = InMemoryDeduplicator()
    events_received: list[ParsedEvent] = []

    async def on_event(ev: ParsedEvent) -> None:
        events_received.append(ev)

    router = build_router(APP_SECRET, VERIFY_TOKEN, dedup, on_event)
    app = FastAPI()
    app.include_router(router)

    body = b'{"entry": []}'
    headers = {"x-hub-signature-256": header_value} if header_value is not None else {}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/webhook/meta", content=body, headers=headers)
        assert res.status_code == 403
        assert len(events_received) == 0


@pytest.mark.asyncio
async def test_post_valid_signature_accepted():
    dedup = InMemoryDeduplicator()
    events_received: list[ParsedEvent] = []

    async def on_event(ev: ParsedEvent) -> None:
        events_received.append(ev)

    router = build_router(APP_SECRET, VERIFY_TOKEN, dedup, on_event)
    app = FastAPI()
    app.include_router(router)

    body = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.valid1",
                                        "from": "447123456789",
                                        "timestamp": "1726660000",
                                        "type": "text",
                                        "text": {"body": "Hi there"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode("utf-8")

    sig = compute_sig(APP_SECRET, body)
    headers = {"x-hub-signature-256": sig}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/webhook/meta", content=body, headers=headers)
        assert res.status_code == 200
        assert len(events_received) == 1
        assert events_received[0].provider_message_id == "wamid.valid1"
        assert events_received[0].text == "Hi there"


# --- 5. Deduplication in POST Tests ---


@pytest.mark.asyncio
async def test_duplicate_provider_message_id_invokes_on_event_exactly_once():
    dedup = InMemoryDeduplicator()
    call_count = 0

    async def on_event(ev: ParsedEvent) -> None:
        nonlocal call_count
        call_count += 1

    router = build_router(APP_SECRET, VERIFY_TOKEN, dedup, on_event)
    app = FastAPI()
    app.include_router(router)

    # First POST
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.duplicate_test",
                                    "from": "447123",
                                    "timestamp": "1726660000",
                                    "type": "text",
                                    "text": {"body": "Hello"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"x-hub-signature-256": compute_sig(APP_SECRET, body)}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # First delivery -> 200, on_event called
        res1 = await client.post("/webhook/meta", content=body, headers=headers)
        assert res1.status_code == 200
        assert call_count == 1

        # Redelivery of identical message -> 200, on_event NOT called again
        res2 = await client.post("/webhook/meta", content=body, headers=headers)
        assert res2.status_code == 200
        assert call_count == 1

        # Intra-batch duplicates: two identical message IDs in one payload
        batch_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.batch_dup",
                                        "from": "447123",
                                        "timestamp": "1726660001",
                                        "type": "text",
                                        "text": {"body": "First"},
                                    },
                                    {
                                        "id": "wamid.batch_dup",
                                        "from": "447123",
                                        "timestamp": "1726660002",
                                        "type": "text",
                                        "text": {"body": "Second"},
                                    },
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        batch_body = json.dumps(batch_payload).encode("utf-8")
        batch_headers = {"x-hub-signature-256": compute_sig(APP_SECRET, batch_body)}

        res3 = await client.post("/webhook/meta", content=batch_body, headers=batch_headers)
        assert res3.status_code == 200
        assert call_count == 2  # wamid.duplicate_test (1) + wamid.batch_dup (1) = 2


# --- 6. Parsing Webhook Types Tests ---


def test_parse_webhook_all_supported_types():
    payload = {
        "entry": [
            {
                "id": "WABA_ID_1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                # 1. Text message
                                {
                                    "id": "wamid.text",
                                    "from": "447111111111",
                                    "timestamp": "1726661001",
                                    "type": "text",
                                    "text": {"body": "Here is some text"},
                                },
                                # 2. Document message
                                {
                                    "id": "wamid.doc",
                                    "from": "447111111111",
                                    "timestamp": "1726661002",
                                    "type": "document",
                                    "document": {
                                        "id": "media_doc_999",
                                        "mime_type": "application/pdf",
                                        "filename": "quote_123.pdf",
                                        "caption": "Project quote details",
                                    },
                                },
                                # 3. Image message
                                {
                                    "id": "wamid.img",
                                    "from": "447111111111",
                                    "timestamp": "1726661003",
                                    "type": "image",
                                    "image": {
                                        "id": "media_img_888",
                                        "mime_type": "image/jpeg",
                                        "caption": "Handwritten work order",
                                    },
                                },
                                # 4. Interactive button_reply
                                {
                                    "id": "wamid.btn",
                                    "from": "447111111111",
                                    "timestamp": "1726661004",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {
                                            "id": "approve_quote_btn",
                                            "title": "Approve Quote",
                                        },
                                    },
                                },
                            ],
                            "statuses": [
                                # 5. Status delivered
                                {
                                    "id": "wamid.status_del",
                                    "status": "delivered",
                                    "recipient_id": "447111111111",
                                    "timestamp": "1726661010",
                                },
                                # 6. Status failed with error code
                                {
                                    "id": "wamid.status_fail",
                                    "status": "failed",
                                    "recipient_id": "447111111111",
                                    "timestamp": "1726661020",
                                    "errors": [
                                        {
                                            "code": 131026,
                                            "title": "Message Undeliverable",
                                        }
                                    ],
                                },
                            ],
                        },
                    }
                ],
            }
        ]
    }

    events = parse_webhook(payload)
    assert len(events) == 6

    # 1. Text
    assert events[0].kind == "message"
    assert events[0].provider_message_id == "wamid.text"
    assert events[0].from_wa_id == "447111111111"
    assert events[0].text == "Here is some text"
    assert events[0].media_id is None
    assert events[0].media_mime is None
    assert events[0].button_payload is None
    assert events[0].status is None
    assert events[0].error_code is None
    assert events[0].timestamp == datetime.fromtimestamp(1726661001, tz=timezone.utc)

    # 2. Document
    assert events[1].kind == "message"
    assert events[1].provider_message_id == "wamid.doc"
    assert events[1].from_wa_id == "447111111111"
    assert events[1].media_id == "media_doc_999"
    assert events[1].media_mime == "application/pdf"
    assert events[1].text == "Project quote details"

    # 3. Image
    assert events[2].kind == "message"
    assert events[2].provider_message_id == "wamid.img"
    assert events[2].from_wa_id == "447111111111"
    assert events[2].media_id == "media_img_888"
    assert events[2].media_mime == "image/jpeg"
    assert events[2].text == "Handwritten work order"

    # 4. Interactive button reply
    assert events[3].kind == "message"
    assert events[3].provider_message_id == "wamid.btn"
    assert events[3].from_wa_id == "447111111111"
    assert events[3].button_payload == "approve_quote_btn"
    assert events[3].text == "Approve Quote"

    # 5. Status delivered
    assert events[4].kind == "status"
    assert events[4].provider_message_id == "wamid.status_del"
    assert events[4].from_wa_id == "447111111111"
    assert events[4].status == "delivered"
    assert events[4].error_code is None

    # 6. Status failed
    assert events[5].kind == "status"
    assert events[5].provider_message_id == "wamid.status_fail"
    assert events[5].from_wa_id == "447111111111"
    assert events[5].status == "failed"
    assert events[5].error_code == 131026


def test_parse_webhook_quick_reply_button():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.quick_btn",
                                    "from": "447000",
                                    "timestamp": "1726661000",
                                    "type": "button",
                                    "button": {
                                        "payload": "yes_payload",
                                        "text": "Yes, please",
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    events = parse_webhook(payload)
    assert len(events) == 1
    assert events[0].provider_message_id == "wamid.quick_btn"
    assert events[0].button_payload == "yes_payload"
    assert events[0].text == "Yes, please"


def test_unknown_message_types_skipped_without_raising():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"id": "m_loc", "type": "location", "location": {"lat": 51.5}},
                                {"id": "m_stk", "type": "sticker", "sticker": {"id": "stk1"}},
                                {"id": "m_aud", "type": "audio", "audio": {"id": "aud1"}},
                                {"id": "m_vid", "type": "video", "video": {"id": "vid1"}},
                                {"id": "m_react", "type": "reaction", "reaction": {"emoji": "👍"}},
                                {
                                    "id": "m_list",
                                    "type": "interactive",
                                    "interactive": {"type": "list_reply"},
                                },
                                {"id": "m_unrec", "type": "completely_unrecognized_type"},
                                {"missing_id": True, "type": "text"},
                                {
                                    "id": "m_valid",
                                    "type": "text",
                                    "text": {"body": "This one is valid"},
                                },
                            ],
                            "statuses": [
                                {"missing_id": True, "status": "sent"},
                                {"id": "s_valid", "status": "sent"},
                            ],
                        }
                    }
                ]
            }
        ]
    }
    # Must not raise, and should only extract the 2 valid items
    events = parse_webhook(payload)
    assert len(events) == 2
    assert events[0].provider_message_id == "m_valid"
    assert events[1].provider_message_id == "s_valid"


# --- 7. Resiliency & Error Handling Tests ---


@pytest.mark.asyncio
async def test_on_event_raising_still_yields_200():
    dedup = InMemoryDeduplicator()
    processed: list[str] = []

    async def on_event(ev: ParsedEvent) -> None:
        processed.append(ev.provider_message_id)
        if ev.provider_message_id == "fail_msg":
            raise RuntimeError("Database or downstream handler failed catastrophically!")

    router = build_router(APP_SECRET, VERIFY_TOKEN, dedup, on_event)
    app = FastAPI()
    app.include_router(router)

    # Batch with 3 events: 1 normal, 1 failing, 1 subsequent
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"id": "msg_1", "type": "text", "text": {"body": "first"}},
                                {"id": "fail_msg", "type": "text", "text": {"body": "fail"}},
                                {"id": "msg_2", "type": "text", "text": {"body": "second"}},
                            ]
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"x-hub-signature-256": compute_sig(APP_SECRET, body)}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/webhook/meta", content=body, headers=headers)
        # MUST return 200 even though on_event raised
        assert res.status_code == 200
        # All 3 events were dispatched to on_event
        assert processed == ["msg_1", "fail_msg", "msg_2"]


@pytest.mark.asyncio
async def test_post_empty_entry_or_useless_body_yields_200():
    dedup = InMemoryDeduplicator()
    router = build_router(APP_SECRET, VERIFY_TOKEN, dedup, MagicMock())
    app = FastAPI()
    app.include_router(router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Empty entry list
        body1 = b'{"entry": []}'
        res1 = await client.post(
            "/webhook/meta",
            content=body1,
            headers={"x-hub-signature-256": compute_sig(APP_SECRET, body1)},
        )
        assert res1.status_code == 200

        # Empty dict
        body2 = b"{}"
        res2 = await client.post(
            "/webhook/meta",
            content=body2,
            headers={"x-hub-signature-256": compute_sig(APP_SECRET, body2)},
        )
        assert res2.status_code == 200

        # Completely empty body
        body3 = b""
        res3 = await client.post(
            "/webhook/meta",
            content=body3,
            headers={"x-hub-signature-256": compute_sig(APP_SECRET, body3)},
        )
        assert res3.status_code == 200

        # Non-dict JSON (e.g. array)
        body4 = b"[]"
        res4 = await client.post(
            "/webhook/meta",
            content=body4,
            headers={"x-hub-signature-256": compute_sig(APP_SECRET, body4)},
        )
        assert res4.status_code == 200

        # Malformed JSON with valid signature
        body5 = b"not json at all"
        res5 = await client.post(
            "/webhook/meta",
            content=body5,
            headers={"x-hub-signature-256": compute_sig(APP_SECRET, body5)},
        )
        assert res5.status_code == 200


# --- 8. Security & Secret Leakage Prevention Tests ---


@pytest.mark.asyncio
async def test_no_secret_or_token_or_signature_in_logs(caplog: pytest.LogCaptureFixture):
    dedup = InMemoryDeduplicator()

    async def on_event(ev: ParsedEvent) -> None:
        raise ValueError("Simulated handler failure")

    router = build_router(APP_SECRET, VERIFY_TOKEN, dedup, on_event)
    app = FastAPI()
    app.include_router(router)

    transport = httpx.ASGITransport(app=app)
    caplog.set_level(logging.DEBUG)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Invalid GET token
        await client.get(
            "/webhook/meta",
            params={"hub.verify_token": "attacker_token_xyz", "hub.challenge": "ch1"},
        )

        # 2. Invalid POST signature
        fake_sig = "sha256=1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
        await client.post(
            "/webhook/meta",
            content=b'{"entry": []}',
            headers={"x-hub-signature-256": fake_sig},
        )

        # 3. Valid POST that triggers an on_event exception
        body = json.dumps(
            {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "messages": [
                                        {
                                            "id": "m_log_test",
                                            "type": "text",
                                            "text": {"body": "hi"},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ]
            }
        ).encode("utf-8")
        valid_sig = compute_sig(APP_SECRET, body)
        await client.post(
            "/webhook/meta",
            content=body,
            headers={"x-hub-signature-256": valid_sig},
        )

    webhook_log_text = "\n".join(
        f"{record.levelname} {record.getMessage()}"
        for record in caplog.records
        if record.name.startswith("winch.")
    )

    # Verify that secrets, verify tokens, and signature hashes NEVER appear in winch log output
    assert APP_SECRET not in webhook_log_text
    assert VERIFY_TOKEN not in webhook_log_text
    assert "attacker_token_xyz" not in webhook_log_text
    assert fake_sig not in webhook_log_text
    assert valid_sig not in webhook_log_text
    assert valid_sig[7:] not in webhook_log_text
