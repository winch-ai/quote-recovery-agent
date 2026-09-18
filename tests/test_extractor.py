"""Tests for the Quote Extractor (src/winch/media.py and src/winch/llm/azure.py).

All tests are fully offline and use httpx.MockTransport.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from winch.llm.azure import SYSTEM_PROMPT, AzureExtractor
from winch.media import MediaError, pdf_to_pngs
from winch.state import QuoteDraft

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PDF = ROOT / "fixtures" / "quotes" / "uk_fencing.pdf"
MULTIPAGE_PDF = ROOT / "Evaluating Trade Quote Recovery Opportunity.pdf"

TEST_ENDPOINT = "https://test-resource.openai.azure.com"
TEST_KEY = "test-azure-api-key-999888777"
TEST_DEPLOYMENT = "gpt-4o"


def _make_azure_response(draft_dict: dict | None = None, raw_content: str | None = None) -> dict:
    if raw_content is not None:
        content = raw_content
    elif draft_dict is not None:
        content = json.dumps(draft_dict)
    else:
        content = json.dumps({
            "customer_name": "Mark Henderson",
            "customer_phone": "07700 900412",
            "customer_email": "mark@example.com",
            "project_title": "2km stock fencing",
            "scope_summary": "Supply and erect 2km stock fencing",
            "quote_total": 24504.0,
            "currency": "GBP",
            "expiry_date": "2026-10-11",
        })
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                }
            }
        ],
        "usage": {"total_tokens": 120},
    }


# =========================================================================
# PDF Rasterisation Tests (pdf_to_pngs)
# =========================================================================

class TestPdfToPngs:
    def test_pdf_to_pngs_on_real_fixture(self):
        """pdf_to_pngs on a real fixture returns at least one non-empty PNG."""
        pdf_bytes = FIXTURE_PDF.read_bytes()
        pngs = pdf_to_pngs(pdf_bytes, dpi=150, max_pages=5)
        assert len(pngs) >= 1
        for png in pngs:
            assert len(png) > 0
            # Check PNG magic bytes
            assert png.startswith(b"\x89PNG\r\n\x1a\n")

    def test_pdf_to_pngs_garbage_bytes_raises_media_error(self):
        """pdf_to_pngs on garbage bytes raises MediaError."""
        with pytest.raises(MediaError, match="pdftoppm failed"):
            pdf_to_pngs(b"this is definitely not a pdf")

    def test_pdf_to_pngs_empty_bytes_raises_media_error(self):
        """pdf_to_pngs on empty bytes raises MediaError."""
        with pytest.raises(MediaError, match="empty PDF bytes"):
            pdf_to_pngs(b"")

    def test_pdf_to_pngs_max_pages_respected(self):
        """max_pages is respected and caps rasterised pages."""
        pdf_bytes = MULTIPAGE_PDF.read_bytes()
        pngs_1 = pdf_to_pngs(pdf_bytes, max_pages=1)
        assert len(pngs_1) == 1

        pngs_3 = pdf_to_pngs(pdf_bytes, max_pages=3)
        assert len(pngs_3) == 3

    def test_pdf_to_pngs_invalid_max_pages_raises(self):
        """max_pages < 1 raises MediaError."""
        pdf_bytes = FIXTURE_PDF.read_bytes()
        with pytest.raises(MediaError, match="max_pages must be at least 1"):
            pdf_to_pngs(pdf_bytes, max_pages=0)

    def test_pdf_to_pngs_cleans_up_temp_files(self, tmp_path_factory):
        """pdf_to_pngs must not leave temp files behind, including on failure."""
        import tempfile

        base_tmp = Path(tempfile.gettempdir())
        before = set(base_tmp.iterdir())

        # Successful run
        pdf_bytes = FIXTURE_PDF.read_bytes()
        pdf_to_pngs(pdf_bytes)
        after_success = set(base_tmp.iterdir())
        new_after_success = after_success - before
        assert not any("page" in p.name or "tmp" in p.name for p in new_after_success)

        # Failure run (garbage bytes)
        with pytest.raises(MediaError):
            pdf_to_pngs(b"corrupted pdf data")

        after_failure = set(base_tmp.iterdir())
        new_after_failure = after_failure - before
        assert not any("page" in p.name or "tmp" in p.name for p in new_after_failure)


# =========================================================================
# AzureExtractor Tests
# =========================================================================

class TestAzureExtractor:
    @pytest.mark.asyncio
    async def test_well_formed_response_parses_into_quote_draft(self):
        """A well-formed response parses into a validated QuoteDraft."""
        recorded_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded_requests.append(request)
            return httpx.Response(
                status_code=200,
                json=_make_azure_response({
                    "customer_name": "Mark Henderson",
                    "customer_phone": "07700 900412",
                    "customer_email": "mark@example.com",
                    "project_title": "2km stock fencing",
                    "scope_summary": "Supply and erect 2km stock fencing",
                    "quote_total": 24504.0,
                    "currency": "GBP",
                    "expiry_date": "2026-10-11",
                }),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        pdf_bytes = FIXTURE_PDF.read_bytes()
        draft = await extractor.extract_quote(pdf_bytes, "application/pdf")

        assert isinstance(draft, QuoteDraft)
        assert draft.customer_name == "Mark Henderson"
        assert draft.customer_phone == "07700 900412"
        assert draft.customer_email == "mark@example.com"
        assert draft.project_title == "2km stock fencing"
        assert draft.scope_summary == "Supply and erect 2km stock fencing"
        assert draft.quote_total == 24504.0
        assert draft.currency == "GBP"
        assert draft.expiry_date == "2026-10-11"

        # Assert request structure
        assert len(recorded_requests) == 1
        req = recorded_requests[0]
        assert req.headers["api-key"] == TEST_KEY
        assert req.headers["content-type"] == "application/json"
        assert "deployments/gpt-4o/chat/completions" in str(req.url)

        body = json.loads(req.content)
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"] == SYSTEM_PROMPT
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True

    @pytest.mark.asyncio
    async def test_customer_phone_null_yields_none(self):
        """A response with customer_phone: null yields None, not a string 'null'."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_make_azure_response({
                    "customer_name": "Denise Okafor",
                    "customer_phone": None,  # serializes to JSON null
                    "customer_email": "denise@example.com",
                    "project_title": "18m x 24m machinery shed",
                    "scope_summary": "Machinery shed construction",
                    "quote_total": 82840.0,
                    "currency": "AUD",
                    "expiry_date": "2026-09-25",
                }),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        draft = await extractor.extract_quote(b"fake_image_bytes", "image/png")
        assert draft.customer_phone is None
        assert draft.customer_phone != "null"
        assert draft.customer_name == "Denise Okafor"
        assert draft.quote_total == 82840.0

    @pytest.mark.asyncio
    async def test_malformed_json_response_raises_media_error(self):
        """Malformed response content raises MediaError."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_make_azure_response(raw_content="not a valid json string {missing brace"),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        with pytest.raises(MediaError, match="Response failed QuoteDraft validation"):
            await extractor.extract_quote(b"image_bytes", "image/png")

    @pytest.mark.asyncio
    async def test_schema_violating_response_raises_media_error(self):
        """Response missing required project_title raises MediaError."""
        def handler(request: httpx.Request) -> httpx.Response:
            # project_title is required by QuoteDraft
            return httpx.Response(
                status_code=200,
                json=_make_azure_response(raw_content=json.dumps({"customer_name": "John Doe"})),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        with pytest.raises(MediaError, match="Response failed QuoteDraft validation"):
            await extractor.extract_quote(b"image_bytes", "image/png")

    @pytest.mark.asyncio
    async def test_extra_fields_in_response_raises_media_error(self):
        """Response with extra fields forbidden by QuoteDraft raises MediaError."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_make_azure_response({
                    "project_title": "Fence repair",
                    "forbidden_extra": "not allowed",
                }),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        with pytest.raises(MediaError, match="Response failed QuoteDraft validation"):
            await extractor.extract_quote(b"image_bytes", "image/png")

    @pytest.mark.asyncio
    async def test_null_or_missing_llm_content_raises_media_error(self):
        """choices[0].message.content being null raises MediaError."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"role": "assistant", "content": None}}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        with pytest.raises(MediaError, match="null content"):
            await extractor.extract_quote(b"image_bytes", "image/png")

    @pytest.mark.asyncio
    async def test_retry_429_then_200_succeeds(self):
        """429 then 200 succeeds with backoff."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(status_code=429, text="Rate limit exceeded")
            return httpx.Response(
                status_code=200,
                json=_make_azure_response({"project_title": "Gate install"}),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
            retry_delay=0.01,
        )

        with patch("winch.llm.azure.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            draft = await extractor.extract_quote(b"image_bytes", "image/png")
            assert draft.project_title == "Gate install"
            assert call_count == 2
            mock_sleep.assert_awaited_once_with(0.01)

    @pytest.mark.asyncio
    async def test_three_consecutive_429_raises_media_error(self):
        """Three consecutive 429s exhausts retries and raises MediaError."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(status_code=429, text="Rate limit exceeded")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
            retry_delay=0.1,
        )

        with patch("winch.llm.azure.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(MediaError, match="HTTP 429 after 3 attempts"):
                await extractor.extract_quote(b"image_bytes", "image/png")

            assert call_count == 3
            assert mock_sleep.await_count == 2
            # Verify exponential backoff: 0.1 * 2^0 = 0.1, 0.1 * 2^1 = 0.2
            mock_sleep.assert_any_await(0.1)
            mock_sleep.assert_any_await(0.2)

    @pytest.mark.asyncio
    async def test_retry_5xx_then_200_succeeds(self):
        """503 then 200 succeeds with backoff."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(status_code=503, text="Service Unavailable")
            return httpx.Response(
                status_code=200,
                json=_make_azure_response({"project_title": "Roof repair"}),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
            retry_delay=0.01,
        )

        with patch("winch.llm.azure.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            draft = await extractor.extract_quote(b"image_bytes", "image/jpeg")
            assert draft.project_title == "Roof repair"
            assert call_count == 2
            mock_sleep.assert_awaited_once_with(0.01)

    @pytest.mark.asyncio
    async def test_400_is_not_retried(self):
        """A 400 is NOT retried (assert the transport was called exactly once)."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(status_code=400, text="Bad Request - Invalid parameter")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
            retry_delay=0.01,
        )

        with pytest.raises(MediaError, match="HTTP 400"):
            await extractor.extract_quote(b"image_bytes", "image/png")

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_unsupported_mime_type_raises_media_error(self):
        """An unsupported mime_type raises MediaError without making network calls."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(status_code=200, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        for bad_mime in ["text/plain", "application/json", "image/gif", "image/webp"]:
            with pytest.raises(MediaError, match="Unsupported mime_type"):
                await extractor.extract_quote(b"some_bytes", bad_mime)

        assert call_count == 0

    @pytest.mark.asyncio
    async def test_api_key_does_not_appear_in_exception_or_repr(self):
        """The API key must never appear in a log line, an exception message, or a repr."""
        secret_key = "sk-super-secret-azure-key-xyz12345"

        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=secret_key,
            deployment=TEST_DEPLOYMENT,
        )

        # 1. repr and str check
        assert secret_key not in repr(extractor)
        assert secret_key not in str(extractor)

        # 2. HTTP error body echoing the key
        def echo_key_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=400,
                text=f"Authentication error: Key {secret_key} is invalid.",
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(echo_key_handler))
        extractor.http_client = client

        with pytest.raises(MediaError) as exc_info:
            await extractor.extract_quote(b"image_bytes", "image/png")

        error_message = str(exc_info.value)
        assert secret_key not in error_message
        assert "***REDACTED***" in error_message

    def test_system_prompt_matches_exact_contract(self):
        """The system prompt must match the exact specification character-for-character."""
        expected = (
            "You extract structured data from trade quotes for a follow-up system.\n\n"
            "Rules that matter more than completeness:\n"
            "- Return null for anything not present. A null is always better than a guess.\n"
            "- customer_phone is the CUSTOMER's number. Quotes almost always also show the\n"
            "  contractor's own phone in the letterhead - never return that one. If only the\n"
            "  contractor's number appears, return null.\n"
            "- quote_total is the single headline figure the customer pays. Do NOT sum stage\n"
            "  payments. Do NOT add optional extras offered as alternatives. Where a gross\n"
            "  (tax-inclusive) figure is shown, return the gross figure.\n"
            "- project_title is ALWAYS required - never null. It is a short human description\n"
            "  of the job, e.g. \"2km stock fencing\". If the document has no title line, derive\n"
            "  it from the line items. This is the one field you must always fill.\n"
            "- The document is the only source. Do not infer, calculate or complete anything."
        )
        assert SYSTEM_PROMPT == expected

    @pytest.mark.asyncio
    async def test_jpeg_mime_type_accepted(self):
        """image/jpeg is an accepted mime type and sent as data:image/jpeg."""
        recorded_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded_requests.append(request)
            return httpx.Response(
                status_code=200,
                json=_make_azure_response({"project_title": "Paving driveway"}),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        extractor = AzureExtractor(
            endpoint=TEST_ENDPOINT,
            api_key=TEST_KEY,
            deployment=TEST_DEPLOYMENT,
            http_client=client,
        )

        draft = await extractor.extract_quote(b"\xff\xd8\xff\xe0jpegdata", "image/jpeg")
        assert draft.project_title == "Paving driveway"
        assert len(recorded_requests) == 1
        body = json.loads(recorded_requests[0].content)
        content_parts = body["messages"][1]["content"]
        image_parts = [p for p in content_parts if p["type"] == "image_url"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
