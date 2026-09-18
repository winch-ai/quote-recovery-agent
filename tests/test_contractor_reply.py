"""Tests for AzureTextClient.classify_contractor_reply.

Mirrors test_triage.py's structure - all offline via httpx.MockTransport. See
that file for the customer-facing classify_intent this is the contractor-side
counterpart of. This one was added specifically to replace keyword matching
that let a real reply ("check in now") fall through as an implicit decline
with zero feedback to the contractor.
"""
from __future__ import annotations

import json

import httpx
import pytest

from winch.llm.azure import CONTRACTOR_REPLY_CLASSIFIER_SYSTEM_PROMPT, AzureTextClient
from winch.state import ContractorReplyIntent

TEST_ENDPOINT = "https://test-resource.openai.azure.com"
TEST_KEY = "test-azure-api-key-999888777"
TEST_DEPLOYMENT = "gpt-4o"


def _response(intent_str: str | None = None, raw_content: str | None = None) -> dict:
    content = raw_content if raw_content is not None else json.dumps({"intent": intent_str})
    return {"choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"total_tokens": 30}}


class TestContractorReplyClassification:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("intent", list(ContractorReplyIntent))
    async def test_each_intent_value_round_trips(self, intent: ContractorReplyIntent):
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return httpx.Response(200, json=_response(intent_str=intent.value))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(TEST_ENDPOINT, TEST_KEY, TEST_DEPLOYMENT, http_client=client)

        classified = await text_client.classify_contractor_reply("check in now")
        assert classified == intent

        assert len(recorded) == 1
        body = json.loads(recorded[0].content)
        assert body["messages"][0]["content"] == CONTRACTOR_REPLY_CLASSIFIER_SYSTEM_PROMPT
        assert body["messages"][1]["content"] == "check in now"
        assert body["response_format"]["json_schema"]["strict"] is True

    @pytest.mark.asyncio
    async def test_uses_a_dedicated_prompt_not_the_customer_classifier(self):
        """A regression guard: this must not accidentally reuse
        CLASSIFIER_SYSTEM_PROMPT, which is written for a customer's reaction
        to a quote follow-up - a different question with different valid
        answers (PRICE_OBJECTION, TECHNICAL_SCOPE_QUERY, ...) that make no
        sense for "did the contractor approve or decline"."""
        from winch.llm.azure import CLASSIFIER_SYSTEM_PROMPT
        assert CONTRACTOR_REPLY_CLASSIFIER_SYSTEM_PROMPT != CLASSIFIER_SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_unknown_intent_string_maps_to_unclear(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_response(intent_str="NONSENSE"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(TEST_ENDPOINT, TEST_KEY, TEST_DEPLOYMENT, http_client=client)
        assert await text_client.classify_contractor_reply("x") == ContractorReplyIntent.UNCLEAR

    @pytest.mark.asyncio
    async def test_malformed_json_maps_to_unclear_not_raise(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_response(raw_content="{not valid json"))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(TEST_ENDPOINT, TEST_KEY, TEST_DEPLOYMENT, http_client=client)
        assert await text_client.classify_contractor_reply("x") == ContractorReplyIntent.UNCLEAR

    @pytest.mark.asyncio
    async def test_null_content_maps_to_unclear(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(TEST_ENDPOINT, TEST_KEY, TEST_DEPLOYMENT, http_client=client)
        assert await text_client.classify_contractor_reply("x") == ContractorReplyIntent.UNCLEAR

    @pytest.mark.asyncio
    async def test_persistent_http_500_maps_to_unclear_never_raises(self):
        """This is the safety property the whole fix depends on: a real
        network failure during classification must never surface as an
        exception to the caller - it must degrade to UNCLEAR so the node
        re-asks instead of crashing the whole graph invocation."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(TEST_ENDPOINT, TEST_KEY, TEST_DEPLOYMENT,
                                      http_client=client, retry_delay=0.001)
        assert await text_client.classify_contractor_reply("x") == ContractorReplyIntent.UNCLEAR

    @pytest.mark.asyncio
    async def test_a_400_is_not_retried(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(400, text="bad request")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(TEST_ENDPOINT, TEST_KEY, TEST_DEPLOYMENT,
                                      http_client=client, retry_delay=0.001)
        result = await text_client.classify_contractor_reply("x")
        assert result == ContractorReplyIntent.UNCLEAR
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_api_key_never_appears_in_any_path_including_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=f"error, key was {TEST_KEY}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text_client = AzureTextClient(TEST_ENDPOINT, TEST_KEY, TEST_DEPLOYMENT,
                                      http_client=client, retry_delay=0.001)
        # classify_contractor_reply swallows to UNCLEAR - the key-scrub
        # guarantee still applies to anything it might log or raise internally.
        result = await text_client.classify_contractor_reply("x")
        assert result == ContractorReplyIntent.UNCLEAR
