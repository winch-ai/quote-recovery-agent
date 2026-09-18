"""Tests for Meta WhatsApp Cloud API outbound channel and media download."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from winch.channels.whatsapp import ChannelError, WhatsAppChannel
from winch.protocols import ChannelAdapter, SendResult

PHONE_NUMBER_ID = "10987654321"
ACCESS_TOKEN = "TEST_WHATSAPP_TOKEN_12345"


@pytest.fixture
def make_channel():
    """Factory fixture for WhatsAppChannel with mock transport."""

    def _factory(
        handler,
        window_checker=None,
        phone_number_id=PHONE_NUMBER_ID,
        access_token=ACCESS_TOKEN,
        language_code="en_GB",
        graph_version="v23.0",
        backoff_base=0.001,
    ):
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        return WhatsAppChannel(
            phone_number_id=phone_number_id,
            access_token=access_token,
            language_code=language_code,
            window_checker=window_checker,
            http_client=client,
            graph_version=graph_version,
            backoff_base=backoff_base,
        )

    return _factory


# --- Protocol Conformance ---


def test_satisfies_channel_adapter_protocol(make_channel):
    channel = make_channel(lambda r: httpx.Response(200))
    assert isinstance(channel, ChannelAdapter)
    assert channel.name == "whatsapp"


# --- Template Sending Tests ---


@pytest.mark.asyncio
async def test_send_template_builds_exact_json_shape_and_extracts_id(make_channel):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "contacts": [{"input": "447123456789", "wa_id": "447123456789"}],
                "messages": [{"id": "wamid.HBgLMTIzNDU2"}],
            },
        )

    channel = make_channel(handler)
    result = await channel.send_template(
        to="447123456789",
        template_name="quote_followup",
        variables=["Alice", "£1,200"],
    )

    assert len(requests) == 1
    req = requests[0]
    assert req.method == "POST"
    assert str(req.url) == f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
    assert req.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"

    payload = json.loads(req.content.decode("utf-8"))
    assert payload == {
        "messaging_product": "whatsapp",
        "to": "447123456789",
        "type": "template",
        "template": {
            "name": "quote_followup",
            "language": {"code": "en_GB"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Alice"},
                        {"type": "text", "text": "£1,200"},
                    ],
                }
            ],
        },
    }

    assert isinstance(result, SendResult)
    assert result.ok is True
    assert result.provider_message_id == "wamid.HBgLMTIzNDU2"
    assert result.error_code is None
    assert result.unreachable is False


@pytest.mark.asyncio
async def test_send_template_empty_variables_omits_components(make_channel):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"messages": [{"id": "wamid.EMPTYVARS"}]},
        )

    channel = make_channel(handler)
    result = await channel.send_template(
        to="447123456789",
        template_name="dormant_quote_intro",
        variables=[],
    )

    assert len(requests) == 1
    payload = json.loads(requests[0].content.decode("utf-8"))
    assert "components" not in payload["template"]
    assert payload["template"] == {
        "name": "dormant_quote_intro",
        "language": {"code": "en_GB"},
    }
    assert result.ok is True
    assert result.provider_message_id == "wamid.EMPTYVARS"


# --- Error Handling Tests ---


@pytest.mark.asyncio
async def test_error_131026_yields_unreachable_true(make_channel):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "(#131026) Message undeliverable",
                    "type": "OAuthException",
                    "code": 131026,
                    "error_data": {"details": "Message Undeliverable."},
                    "error_subcode": 2494011,
                    "fbtrace_id": "A1B2C3D4",
                }
            },
        )

    channel = make_channel(handler)
    result = await channel.send_template("447123456789", "intro", [])

    assert result.ok is False
    assert result.error_code == 131026
    assert result.unreachable is True
    assert result.provider_message_id is None


@pytest.mark.asyncio
async def test_another_error_code_yields_unreachable_false(make_channel):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid template name",
                    "type": "OAuthException",
                    "code": 100,
                }
            },
        )

    channel = make_channel(handler)
    result = await channel.send_template("447123456789", "invalid_template", [])

    assert result.ok is False
    assert result.error_code == 100
    assert result.unreachable is False
    assert result.provider_message_id is None


# --- 24-Hour Service Window (Fail-Closed) Tests ---


@pytest.mark.asyncio
async def test_send_freeform_no_window_checker_makes_zero_http_calls(make_channel):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"messages": [{"id": "wamid.FAIL"}]})

    channel = make_channel(handler, window_checker=None)
    result = await channel.send_freeform(to="447123456789", body="Hello customer")

    assert call_count == 0
    assert result.ok is False
    assert result.provider_message_id is None


@pytest.mark.asyncio
async def test_send_freeform_checker_returning_false_makes_zero_http_calls(make_channel):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"messages": [{"id": "wamid.FAIL"}]})

    async def closed_checker(to: str) -> bool:
        return False

    channel = make_channel(handler, window_checker=closed_checker)
    result = await channel.send_freeform(to="447123456789", body="Hello customer")

    assert call_count == 0
    assert result.ok is False
    assert result.provider_message_id is None


@pytest.mark.asyncio
async def test_send_freeform_checker_returning_true_does_send(make_channel):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"messages": [{"id": "wamid.FREEFORM_OK"}]},
        )

    async def open_checker(to: str) -> bool:
        return True

    channel = make_channel(handler, window_checker=open_checker)
    result = await channel.send_freeform(to="447123456789", body="Can you do Tuesday?")

    assert len(requests) == 1
    req = requests[0]
    assert req.method == "POST"
    assert str(req.url) == f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
    assert req.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"

    payload = json.loads(req.content.decode("utf-8"))
    assert payload == {
        "messaging_product": "whatsapp",
        "to": "447123456789",
        "type": "text",
        "text": {
            "preview_url": False,
            "body": "Can you do Tuesday?",
        },
    }

    assert result.ok is True
    assert result.provider_message_id == "wamid.FREEFORM_OK"


@pytest.mark.asyncio
async def test_send_freeform_checker_exception_fails_closed(make_channel):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"messages": [{"id": "wamid.FAIL"}]})

    async def broken_checker(to: str) -> bool:
        raise RuntimeError("DB connection lost")

    channel = make_channel(handler, window_checker=broken_checker)
    result = await channel.send_freeform(to="447123456789", body="Hello")

    assert call_count == 0
    assert result.ok is False


@pytest.mark.asyncio
async def test_send_freeform_sync_checker_supported(make_channel):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.SYNC_OK"}]})

    def sync_checker(to: str) -> bool:
        return True

    channel = make_channel(handler, window_checker=sync_checker)
    result = await channel.send_freeform(to="447123456789", body="Sync check message")

    assert len(requests) == 1
    assert result.ok is True
    assert result.provider_message_id == "wamid.SYNC_OK"


# --- Retry and Backoff Tests ---


@pytest.mark.asyncio
async def test_429_then_200_succeeds(make_channel):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, json={"error": {"code": 130429, "message": "Rate limited"}})
        return httpx.Response(200, json={"messages": [{"id": "wamid.RETRY_SUCCESS"}]})

    channel = make_channel(handler)
    result = await channel.send_template("447123456789", "quote_intro", [])

    assert call_count == 2
    assert result.ok is True
    assert result.provider_message_id == "wamid.RETRY_SUCCESS"


@pytest.mark.asyncio
async def test_three_429s_gives_ok_false(make_channel):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(429, json={"error": {"code": 130429, "message": "Rate limited"}})

    channel = make_channel(handler)
    result = await channel.send_template("447123456789", "quote_intro", [])

    assert call_count == 3
    assert result.ok is False
    assert result.error_code == 130429


@pytest.mark.asyncio
async def test_5xx_then_200_succeeds(make_channel):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"messages": [{"id": "wamid.503_RETRY_OK"}]})

    channel = make_channel(handler)
    result = await channel.send_template("447123456789", "quote_intro", [])

    assert call_count == 2
    assert result.ok is True
    assert result.provider_message_id == "wamid.503_RETRY_OK"


@pytest.mark.asyncio
async def test_400_is_not_retried_assert_one_call(make_channel):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            400,
            json={"error": {"code": 100, "message": "Bad request"}},
        )

    channel = make_channel(handler)
    result = await channel.send_template("447123456789", "quote_intro", [])

    assert call_count == 1
    assert result.ok is False
    assert result.error_code == 100


# --- Token Redaction Tests ---


@pytest.mark.asyncio
async def test_access_token_appears_in_no_exception_message(make_channel):
    secret_token = "SECRET_SUPER_CONFIDENTIAL_TOKEN_ABC987"

    def handler(request: httpx.Request) -> httpx.Response:
        # Echoing the token in the error response body
        return httpx.Response(
            401,
            text=f"Authentication failed: token {secret_token} is invalid or expired.",
        )

    channel = make_channel(handler, access_token=secret_token)

    with pytest.raises(ChannelError) as exc_info:
        await channel.download_media("media_token_test")

    err_str = str(exc_info.value)
    assert secret_token not in err_str
    assert "[REDACTED]" in err_str

    # Also verify repr does not expose the token
    assert secret_token not in repr(channel)


# --- Media Download Tests ---


@pytest.mark.asyncio
async def test_download_media_performs_two_step_flow(make_channel):
    requests: list[httpx.Request] = []
    step1_url = f"https://graph.facebook.com/v23.0/media_12345"
    step2_url = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=media_12345"
    fake_image_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == step1_url:
            assert request.method == "GET"
            assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "url": step2_url,
                    "mime_type": "image/jpeg",
                    "sha256": "abc123sha256",
                    "file_size": len(fake_image_bytes),
                    "id": "media_12345",
                },
            )
        elif str(request.url) == step2_url:
            assert request.method == "GET"
            assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
            return httpx.Response(
                200,
                content=fake_image_bytes,
                headers={"Content-Type": "image/jpeg"},
            )
        return httpx.Response(404)

    channel = make_channel(handler)
    data, mime_type = await channel.download_media("media_12345")

    assert len(requests) == 2
    assert data == fake_image_bytes
    assert mime_type == "image/jpeg"


@pytest.mark.asyncio
async def test_download_media_raises_channel_error_on_failed_lookup(make_channel):
    # Step 1 returns 404
    def not_found_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"message": "Media not found", "code": 100}},
        )

    channel = make_channel(not_found_handler)
    with pytest.raises(ChannelError):
        await channel.download_media("missing_media_id")

    # Step 1 returns 200 but missing "url"
    def missing_url_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "media_no_url", "mime_type": "image/jpeg"})

    channel_no_url = make_channel(missing_url_handler)
    with pytest.raises(ChannelError):
        await channel_no_url.download_media("media_no_url")

    # Step 1 succeeds but Step 2 returns 500
    step1_url = f"https://graph.facebook.com/v23.0/step2_fail"
    download_url = "https://lookaside.fbsbx.com/fail_download"

    def step2_fail_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == step1_url:
            return httpx.Response(200, json={"url": download_url, "mime_type": "image/png"})
        return httpx.Response(500, text="Internal Server Error")

    channel_step2_fail = make_channel(step2_fail_handler)
    with pytest.raises(ChannelError):
        await channel_step2_fail.download_media("step2_fail")

    # Empty media id
    with pytest.raises(ChannelError):
        await channel.download_media("")
