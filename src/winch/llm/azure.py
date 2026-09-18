"""Azure OpenAI extractor for quote drafts."""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx

from winch.media import MediaError, pdf_to_pngs
from winch.state import QuoteDraft

SYSTEM_PROMPT = """You extract structured data from trade quotes for a follow-up system.

Rules that matter more than completeness:
- Return null for anything not present. A null is always better than a guess.
- customer_phone is the CUSTOMER's number. Quotes almost always also show the
  contractor's own phone in the letterhead - never return that one. If only the
  contractor's number appears, return null.
- quote_total is the single headline figure the customer pays. Do NOT sum stage
  payments. Do NOT add optional extras offered as alternatives. Where a gross
  (tax-inclusive) figure is shown, return the gross figure.
- project_title is ALWAYS required - never null. It is a short human description
  of the job, e.g. "2km stock fencing". If the document has no title line, derive
  it from the line items. This is the one field you must always fill.
- The document is the only source. Do not infer, calculate or complete anything."""

ACCEPTED_MIME_TYPES = ("application/pdf", "image/png", "image/jpeg")
REQUEST_TIMEOUT = 120.0
MAX_ATTEMPTS = 3


def _build_response_format() -> dict[str, Any]:
    schema = QuoteDraft.model_json_schema()
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}).keys())
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "quote_draft",
            "strict": True,
            "schema": schema,
        },
    }


class AzureExtractor:
    """Satisfies the extract_quote half of LLMClient."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str = "2024-10-21",
        http_client: httpx.AsyncClient | None = None,
        retry_delay: float = 0.5,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.deployment = deployment
        self.api_version = api_version
        self.http_client = http_client
        self._retry_delay = retry_delay

    def __repr__(self) -> str:
        return (
            f"AzureExtractor(endpoint={self.endpoint!r}, "
            f"deployment={self.deployment!r}, "
            f"api_version={self.api_version!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def _scrub(self, text: str) -> str:
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, "***REDACTED***")
        return text

    def _build_url(self) -> str:
        endpoint = self.endpoint.rstrip("/")
        if "/openai/deployments/" in endpoint:
            url = endpoint
            if "api-version=" not in url:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}api-version={self.api_version}"
            return url
        return f"{endpoint}/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"

    async def _send_with_retries(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> httpx.Response:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=REQUEST_TIMEOUT,
                )
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < MAX_ATTEMPTS - 1:
                    delay = self._retry_delay * (2**attempt)
                    await asyncio.sleep(delay)
                    continue
                msg = self._scrub(f"Request failed: {exc}")
                raise MediaError(msg) from None
            except Exception as exc:
                msg = self._scrub(str(exc))
                raise MediaError(msg) from None

            if response.status_code == 200:
                return response

            # 429 and 5xx are retryable
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < MAX_ATTEMPTS - 1:
                    delay = self._retry_delay * (2**attempt)
                    await asyncio.sleep(delay)
                    continue
                detail = self._scrub(response.text)
                raise MediaError(f"HTTP {response.status_code} after {MAX_ATTEMPTS} attempts: {detail}")

            # 4xx other than 429: do not retry
            detail = self._scrub(response.text)
            raise MediaError(f"HTTP {response.status_code}: {detail}")

        raise MediaError("Request failed after retries.")

    async def extract_quote(self, media: bytes, mime_type: str) -> QuoteDraft:
        if mime_type not in ACCEPTED_MIME_TYPES:
            raise MediaError(
                f"Unsupported mime_type: {mime_type!r}. Must be one of: {', '.join(ACCEPTED_MIME_TYPES)}"
            )

        if not media:
            raise MediaError("Media bytes cannot be empty.")

        if mime_type == "application/pdf":
            png_pages = pdf_to_pngs(media)
            images = [("image/png", page) for page in png_pages]
        else:
            images = [(mime_type, media)]

        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": "Extract structured quote data from this document."}
        ]
        for img_mime, img_data in images:
            b64 = base64.b64encode(img_data).decode("ascii")
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{img_mime};base64,{b64}"},
            })

        body = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content_parts},
            ],
            "response_format": _build_response_format(),
            "max_completion_tokens": 1000,
        }

        url = self._build_url()
        headers = {
            "Content-Type": "application/json",
            "api-key": self.api_key,
        }

        if self.http_client is not None:
            response = await self._send_with_retries(self.http_client, url, headers, body)
        else:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await self._send_with_retries(client, url, headers, body)

        try:
            data = response.json()
        except Exception as exc:
            msg = self._scrub(str(exc))
            raise MediaError(f"Failed to parse response JSON: {msg}") from None

        try:
            raw_content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            msg = self._scrub(str(exc))
            raise MediaError(f"Malformed LLM response structure: {msg}") from None

        if raw_content is None:
            raise MediaError("LLM returned null content (refusal or content filter).")

        try:
            draft = QuoteDraft.model_validate_json(raw_content)
        except Exception as exc:
            msg = self._scrub(str(exc))
            raise MediaError(f"Response failed QuoteDraft validation: {msg}") from None

        return draft


from winch.state import Intent, Quote

CLASSIFIER_SYSTEM_PROMPT = """You classify a customer's reply to a trade quote follow-up. Return one intent.

ACCEPTED - they agree, want to proceed, or confirm dates.
PRICE_OBJECTION - they mention cost, budget, a cheaper alternative, or say it is
  too expensive.
QUESTION_ON_TIMELINE - they ask about dates, scheduling, lead time or duration.
TECHNICAL_SCOPE_QUERY - they ask what is included, materials, dimensions,
  methods, or anything requiring the contractor's expertise.
UNSUBSCRIBE - they ask to stop being contacted, or reply STOP.
UNCLEAR - anything else, or you are not confident.

Prefer UNCLEAR over a wrong guess. A misrouted reply is worse than an
unclassified one, because the contractor reads every unclassified reply anyway."""

COMPOSE_REPLY_SYSTEM_PROMPT = (
    "You draft a short, plain reply to a customer in the contractor's voice.\n\n"
    "Rules:\n"
    "- Keep it concise, professional, and friendly.\n"
    "- Never state a price, cost, or figure.\n"
    "- Never state a date, schedule, or time commitment.\n"
    "- Do not make promises or binding commitments."
)


def _build_classifier_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "intent_classification",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "enum": [i.value for i in Intent],
                    },
                },
                "required": ["intent"],
                "additionalProperties": False,
            },
        },
    }


class AzureTextClient:
    """classify_intent and compose_reply. Same Azure deployment as extraction."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str = "2024-10-21",
        http_client: httpx.AsyncClient | None = None,
        retry_delay: float = 0.5,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.deployment = deployment
        self.api_version = api_version
        self.http_client = http_client
        self._retry_delay = retry_delay

    def __repr__(self) -> str:
        return (
            f"AzureTextClient(endpoint={self.endpoint!r}, "
            f"deployment={self.deployment!r}, "
            f"api_version={self.api_version!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def _scrub(self, text: str) -> str:
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, "***REDACTED***")
        return text

    def _build_url(self) -> str:
        endpoint = self.endpoint.rstrip("/")
        if "/openai/deployments/" in endpoint:
            url = endpoint
            if "api-version=" not in url:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}api-version={self.api_version}"
            return url
        return f"{endpoint}/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"

    async def _send_with_retries(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> httpx.Response:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=REQUEST_TIMEOUT,
                )
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < MAX_ATTEMPTS - 1:
                    delay = self._retry_delay * (2**attempt)
                    await asyncio.sleep(delay)
                    continue
                msg = self._scrub(f"Request failed: {exc}")
                raise RuntimeError(msg) from None
            except Exception as exc:
                msg = self._scrub(str(exc))
                raise RuntimeError(msg) from None

            if response.status_code == 200:
                return response

            # 429 and 5xx are retryable
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < MAX_ATTEMPTS - 1:
                    delay = self._retry_delay * (2**attempt)
                    await asyncio.sleep(delay)
                    continue
                detail = self._scrub(response.text)
                raise RuntimeError(f"HTTP {response.status_code} after {MAX_ATTEMPTS} attempts: {detail}")

            # 4xx other than 429: do not retry
            detail = self._scrub(response.text)
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")

        raise RuntimeError("Request failed after retries.")

    async def classify_intent(self, text: str) -> Intent:
        try:
            url = self._build_url()
            headers = {
                "Content-Type": "application/json",
                "api-key": self.api_key,
            }
            body = {
                "messages": [
                    {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "response_format": _build_classifier_response_format(),
                "max_completion_tokens": 50,
            }

            if self.http_client is not None:
                response = await self._send_with_retries(self.http_client, url, headers, body)
            else:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    response = await self._send_with_retries(client, url, headers, body)

            data = response.json()
            raw_content = data["choices"][0]["message"]["content"]
            if raw_content is None:
                return Intent.UNCLEAR
            parsed = json.loads(raw_content)
            intent_val = parsed.get("intent")
            return Intent(intent_val)
        except Exception:
            return Intent.UNCLEAR

    async def compose_reply(self, quote: Quote, customer_message: str) -> str:
        url = self._build_url()
        headers = {
            "Content-Type": "application/json",
            "api-key": self.api_key,
        }
        body = {
            "messages": [
                {"role": "system", "content": COMPOSE_REPLY_SYSTEM_PROMPT},
                {"role": "user", "content": f"Customer message: {customer_message}\nProject: {quote.project_title}"},
            ],
            "max_completion_tokens": 500,
        }

        if self.http_client is not None:
            response = await self._send_with_retries(self.http_client, url, headers, body)
        else:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await self._send_with_retries(client, url, headers, body)

        try:
            data = response.json()
        except Exception as exc:
            msg = self._scrub(str(exc))
            raise RuntimeError(f"Failed to parse response JSON: {msg}") from None

        try:
            raw_content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            msg = self._scrub(str(exc))
            raise RuntimeError(f"Malformed LLM response structure: {msg}") from None

        if raw_content is None:
            raise RuntimeError("LLM returned null content (refusal or content filter).")

        return raw_content.strip()

