"""Tests for AzureTextClient (intent classification and reply composition).

All tests are fully offline and use httpx.MockTransport.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from winch.llm.azure import CLASSIFIER_SYSTEM_PROMPT, AzureTextClient
from winch.state import Intent, Quote

TEST_ENDPOINT = "https://test-resource.openai.azure.com"
TEST_KEY = "test-azure-api-key-999888777"
TEST_DEPLOYMENT = "gpt-4o"


def _make_azure_intent_response(intent_str: str | None = None, raw_content: str | None = None) -> dict:
    if raw_content is not None:
        content = raw_content
    elif intent_str is not None:
        content = json.dumps({"intent": intent_str})
    else:
        content = json.dumps({"intent": Intent.ACCEPTED.value})
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                }
            }
        ],
        "usage": {"total_tokens": 40},
    }


def _make_dummy_quote() -> Quote:
    return Quote(
        quote_id="quote_triage_001",
        customer_name="Mark Henderson",
        customer_phone="07700 900412",
        customer_email="mark@example.com",
        project_title="Stock fencing",
        scope_summary="Fencing work",
        quote_total=24504.0,
        currency="GBP",
        expiry_date="2026-10-11",
    )


class TestIntentClassification:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("intent", list(Intent))
    async def test_each_intent_value_round_trips_from_well_formed_response(self, intent: Intent):
        """each intent value round-trips from a well-formed response."""
        recorded_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded_requests.append(request)
            return httpx.Response(
                status_code=200,
                json=_make_azure_intent_response(intent_str=intent.value),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        classified = await text_client.classify_intent("Customer says something here")
        assert classified == intent

        assert len(recorded_requests) == 1
        req = recorded_requests[0]
        assert req.headers["api-key"] == TEST_KEY
        assert req.headers["content-type"] == "application/json"
        body = json.loads(req.content)
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"] == CLASSIFIER_SYSTEM_PROMPT
        assert body["messages"][1]["role"] == "user"
        assert body["messages"][1]["content"] == "Customer says something here"
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True

    @pytest.mark.asyncio
    async def test_unknown_intent_string_maps_to_unclear(self):
        """an unknown intent string maps to UNCLEAR."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_make_azure_intent_response(intent_str="DEFINITELY_UNKNOWN_INTENT"),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        classified = await text_client.classify_intent("Some ambiguous text")
        assert classified == Intent.UNCLEAR

    @pytest.mark.asyncio
    async def test_malformed_json_maps_to_unclear_does_not_raise(self):
        """malformed JSON maps to UNCLEAR, does not raise."""
        # Case 1: Malformed content inside choice message
        def handler_bad_content(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_make_azure_intent_response(raw_content="not valid json {missing brace"),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler_bad_content))
        text_client = AzureTextClient(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )
        assert await text_client.classify_intent("Hi") == Intent.UNCLEAR

        # Case 2: Response text is not JSON at all
        def handler_bad_json(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=200, text="<!DOCTYPE html><html>502 Bad Gateway</html>")

        client2 = httpx.AsyncClient(transport=httpx.MockTransport(handler_bad_json))
        text_client2 = AzureTextClient(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client2,
        )
        assert await text_client2.classify_intent("Hi") == Intent.UNCLEAR

        # Case 3: Content is None
        def handler_none_content(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"role": "assistant", "content": None}}]},
            )

        client3 = httpx.AsyncClient(transport=httpx.MockTransport(handler_none_content))
        text_client3 = AzureTextClient(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client3,
        )
        assert await text_client3.classify_intent("Hi") == Intent.UNCLEAR

    @pytest.mark.asyncio
    async def test_persistent_http_500_maps_to_unclear_does_not_raise(self):
        """persistent HTTP 500 maps to UNCLEAR, does not raise."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(status_code=500, text="Internal Server Error")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
            retry_delay=0.01,
        )

        with patch("winch.llm.azure.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await text_client.classify_intent("Customer message")
            assert result == Intent.UNCLEAR
            assert call_count == 3
            assert mock_sleep.await_count == 2
            mock_sleep.assert_any_await(0.01)
            mock_sleep.assert_any_await(0.02)

    @pytest.mark.asyncio
    async def test_400_is_not_retried(self):
        """a 400 is not retried."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(status_code=400, text="Bad Request - Unsupported parameter")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
            retry_delay=0.01,
        )

        with patch("winch.llm.azure.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await text_client.classify_intent("Customer message")
            assert result == Intent.UNCLEAR
            assert call_count == 1
            mock_sleep.assert_not_called()

        # Also test compose_reply does not retry 400 and raises
        call_count = 0
        with patch("winch.llm.azure.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            quote = _make_dummy_quote()
            with pytest.raises(RuntimeError, match="HTTP 400"):
                await text_client.compose_reply(quote, "Customer message")
            assert call_count == 1
            mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_key_absent_from_every_exception_message(self):
        """the api key is absent from every exception message."""
        secret_key = "sk-super-secret-azure-key-xyz12345"

        text_client = AzureTextClient(
            endpoint=TEST_ENDPOINT,
            api_key=secret_key,
            deployment=TEST_DEPLOYMENT,
        )

        # 1. repr and str check
        assert secret_key not in repr(text_client)
        assert secret_key not in str(text_client)

        quote = _make_dummy_quote()

        # 2. HTTP 400 error body echoing the key
        def echo_key_handler_400(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=400,
                text=f"Authentication error: Key {secret_key} is invalid.",
            )

        text_client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(echo_key_handler_400))
        with pytest.raises(Exception) as exc_info:
            await text_client.compose_reply(quote, "Hello")

        error_message = str(exc_info.value)
        assert secret_key not in error_message
        assert "***REDACTED***" in error_message

        # 3. HTTP 500 error body echoing the key
        def echo_key_handler_500(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=500,
                text=f"Server error for request with key {secret_key}.",
            )

        text_client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(echo_key_handler_500))
        text_client._retry_delay = 0.001
        with patch("winch.llm.azure.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(Exception) as exc_info:
                await text_client.compose_reply(quote, "Hello")

        error_message = str(exc_info.value)
        assert secret_key not in error_message
        assert "***REDACTED***" in error_message

        # 4. Network error echoing the key
        def network_error_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"Failed to connect using key={secret_key}")

        text_client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(network_error_handler))
        with patch("winch.llm.azure.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(Exception) as exc_info:
                await text_client.compose_reply(quote, "Hello")

        error_message = str(exc_info.value)
        assert secret_key not in error_message
        assert "***REDACTED***" in error_message

    def test_classifier_system_prompt_matches_exact_specification(self):
        """The classifier system prompt must match the exact specification."""
        expected = (
            "You classify a customer's reply to a trade quote follow-up. Return one intent.\n\n"
            "ACCEPTED - they agree, want to proceed, or confirm dates.\n"
            "PRICE_OBJECTION - they mention cost, budget, a cheaper alternative, or say it is\n"
            "  too expensive.\n"
            "QUESTION_ON_TIMELINE - they ask about dates, scheduling, lead time or duration.\n"
            "TECHNICAL_SCOPE_QUERY - they ask what is included, materials, dimensions,\n"
            "  methods, or anything requiring the contractor's expertise.\n"
            "UNSUBSCRIBE - they ask to stop being contacted, or reply STOP.\n"
            "UNCLEAR - anything else, or you are not confident.\n\n"
            "Prefer UNCLEAR over a wrong guess. A misrouted reply is worse than an\n"
            "unclassified one, because the contractor reads every unclassified reply anyway."
        )
        assert CLASSIFIER_SYSTEM_PROMPT == expected

    @pytest.mark.asyncio
    async def test_compose_reply_success(self):
        """compose_reply drafts a short reply in the contractor's voice."""
        quote = _make_dummy_quote()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Thanks for getting back to us. Happy to discuss any questions.",
                            }
                        }
                    ]
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        reply = await text_client.compose_reply(quote, "Can we talk about the fence?")
        assert reply == "Thanks for getting back to us. Happy to discuss any questions."
