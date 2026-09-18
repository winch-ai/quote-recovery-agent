"""Meta WhatsApp Cloud API outbound channel and media download adapter."""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx
from winch.protocols import ChannelAdapter, SendResult

logger = logging.getLogger("winch.channels.whatsapp")


class ChannelError(Exception):
    """Raised on WhatsApp channel communication or media retrieval failure."""


class WhatsAppChannel:
    """Satisfies ChannelAdapter. Meta Cloud API."""

    name = "whatsapp"
    DEFAULT_TIMEOUT: float = 30.0
    MAX_ATTEMPTS: int = 3
    DEFAULT_GRAPH_VERSION: str = "v23.0"
    DEFAULT_LANGUAGE_CODE: str = "en_GB"

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        language_code: str = "en_GB",
        window_checker: Callable[[str], Awaitable[bool]] | None = None,
        http_client: httpx.AsyncClient | None = None,
        graph_version: str = "v23.0",
        *,
        backoff_base: float = 0.01,
    ) -> None:
        """window_checker(to) -> True when a 24-hour window is open for that
        recipient. When None, send_freeform ALWAYS refuses — fail closed."""
        self.phone_number_id = phone_number_id
        self._access_token = access_token
        self.language_code = language_code
        self.window_checker = window_checker
        self.graph_version = graph_version.strip("/")
        self._backoff_base = backoff_base
        self._client = http_client or httpx.AsyncClient(timeout=self.DEFAULT_TIMEOUT)

    @property
    def access_token(self) -> str:
        return self._access_token

    def __repr__(self) -> str:
        return (
            f"WhatsAppChannel(phone_number_id={self.phone_number_id!r}, "
            f"language_code={self.language_code!r}, "
            f"graph_version={self.graph_version!r})"
        )

    def _scrub(self, text: str) -> str:
        """Scrub access token from any text, error message, or log."""
        if not self._access_token:
            return text
        return text.replace(self._access_token, "[REDACTED]")

    @staticmethod
    def _extract_error_code(data: Any) -> int | None:
        if isinstance(data, dict):
            error_info = data.get("error")
            if isinstance(error_info, dict):
                code = error_info.get("code")
                if code is not None:
                    try:
                        return int(code)
                    except (ValueError, TypeError):
                        return None
        return None

    async def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        """Send HTTP request with retries on 429 and 5xx using exponential backoff.

        Max 3 attempts. Never retries other 4xx status codes.
        """
        last_response: httpx.Response | None = None
        last_exc: Exception | None = None

        for attempt in range(self.MAX_ATTEMPTS):
            req_headers = {
                "Authorization": f"Bearer {self._access_token}",
            }
            if headers:
                req_headers.update(headers)

            try:
                response = await self._client.request(
                    method=method,
                    url=url,
                    json=json_data,
                    headers=req_headers,
                    timeout=self.DEFAULT_TIMEOUT,
                )
                last_response = response

                # Retry on 429 and 5xx status codes
                if response.status_code == 429 or (500 <= response.status_code < 600):
                    if attempt < self.MAX_ATTEMPTS - 1:
                        delay = self._backoff_base * (2 ** attempt)
                        await asyncio.sleep(delay)
                        continue
                    # Retries exhausted
                    break

                # 2xx, 3xx, and 4xx (except 429) are not retried
                return response

            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self.MAX_ATTEMPTS - 1:
                    delay = self._backoff_base * (2 ** attempt)
                    await asyncio.sleep(delay)
                    continue
                break

        if last_response is not None:
            return last_response
        if last_exc is not None:
            logger.warning(
                self._scrub(f"Request failed with transport error after retries: {last_exc}")
            )
        return None

    async def _post_message(self, url: str, payload: dict[str, Any]) -> SendResult:
        response = await self._request_with_retries(
            method="POST",
            url=url,
            json_data=payload,
        )

        if response is None:
            return SendResult(ok=False)

        if 200 <= response.status_code < 300:
            try:
                data = response.json()
            except Exception:
                data = {}

            if isinstance(data, dict):
                # Check if Meta returned an error object despite 2xx status
                if "error" in data:
                    error_code = self._extract_error_code(data)
                    return SendResult(
                        ok=False,
                        error_code=error_code,
                        unreachable=(error_code == 131026),
                    )

                messages = data.get("messages")
                if isinstance(messages, list) and len(messages) > 0:
                    first_msg = messages[0]
                    msg_id = first_msg.get("id") if isinstance(first_msg, dict) else None
                    return SendResult(ok=True, provider_message_id=msg_id)

            return SendResult(ok=False)

        # Non-2xx response
        try:
            data = response.json()
        except Exception:
            data = {}

        error_code = self._extract_error_code(data)
        return SendResult(
            ok=False,
            error_code=error_code,
            unreachable=(error_code == 131026),
        )

    async def send_template(
        self, to: str, template_name: str, variables: list[str]
    ) -> SendResult:
        template_payload: dict[str, Any] = {
            "name": template_name,
            "language": {"code": self.language_code},
        }
        if variables:
            template_payload["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(v)} for v in variables],
                }
            ]

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": template_payload,
        }

        url = f"https://graph.facebook.com/{self.graph_version}/{self.phone_number_id}/messages"
        return await self._post_message(url, payload)

    async def send_freeform(self, to: str, body: str) -> SendResult:
        """Only legal inside an open 24-hour window. Implementations MUST refuse
        otherwise rather than silently falling back to a template."""
        if self.window_checker is None:
            return SendResult(ok=False)

        try:
            res = self.window_checker(to)
            if inspect.isawaitable(res):
                is_open = await res
            else:
                is_open = bool(res)
        except Exception:
            logger.error("window_checker raised exception; failing closed")
            return SendResult(ok=False)

        if not is_open:
            return SendResult(ok=False)

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": body,
            },
        }

        url = f"https://graph.facebook.com/{self.graph_version}/{self.phone_number_id}/messages"
        return await self._post_message(url, payload)

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Two-step Meta flow: GET /{media_id} for the URL, then GET that URL
        with the bearer token. Returns (bytes, mime_type). Raises ChannelError."""
        if not media_id:
            raise ChannelError("media_id cannot be empty")

        clean_media_id = media_id.strip("/")
        meta_url = f"https://graph.facebook.com/{self.graph_version}/{clean_media_id}"

        # Step 1: GET /{graph_version}/{media_id} for the URL
        resp1 = await self._request_with_retries(method="GET", url=meta_url)
        if resp1 is None:
            raise ChannelError(
                self._scrub(f"Network error requesting media metadata for {media_id}")
            ) from None

        if resp1.status_code != 200:
            raise ChannelError(
                self._scrub(
                    f"Failed to get media metadata ({resp1.status_code}): {resp1.text}"
                )
            ) from None

        try:
            meta_data = resp1.json()
        except Exception as exc:
            raise ChannelError(
                self._scrub(f"Failed to parse media metadata JSON: {exc}")
            ) from None

        if not isinstance(meta_data, dict):
            raise ChannelError("Invalid media metadata response structure")

        download_url = meta_data.get("url")
        if not download_url or not isinstance(download_url, str):
            raise ChannelError("Media metadata missing download url")

        mime_type = meta_data.get("mime_type")

        # Step 2: GET the download URL with the bearer token
        resp2 = await self._request_with_retries(method="GET", url=download_url)
        if resp2 is None:
            raise ChannelError(
                self._scrub(f"Network error downloading media content for {media_id}")
            ) from None

        if resp2.status_code != 200:
            raise ChannelError(
                self._scrub(
                    f"Failed to download media content ({resp2.status_code}): {resp2.text}"
                )
            ) from None

        content_type = mime_type or resp2.headers.get("content-type", "application/octet-stream")
        if isinstance(content_type, str) and ";" in content_type:
            content_type = content_type.split(";")[0].strip()

        return resp2.content, content_type
