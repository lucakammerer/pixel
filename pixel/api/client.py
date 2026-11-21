"""One httpx client for the 5U API. The key goes into the `X-API-Key` header and nowhere else."""

import asyncio

import httpx

from pixel.config import Settings

RETRY_STATUS = (500, 502, 504)  # 503 is an answer (where to get the rate instead), not an outage
BACKOFF_S = (0.5, 1.0, 2.0)


class ApiError(Exception):
    def __init__(self, status: int, detail: str, retry_after: float | None = None):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


def make_client(settings: Settings) -> httpx.AsyncClient:
    """Base URL, key header, timeouts. Never logs headers."""
    return httpx.AsyncClient(
        base_url=settings.api_base, headers={"X-API-Key": settings.api_key}, timeout=httpx.Timeout(15.0, connect=5.0)
    )


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return str(detail)[:500]


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


async def request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
    """Sends one call; retries connection errors and 5xx with backoff; raises ApiError for the rest.
    A POST is retried only when it never reached the server, so a mail is not sent twice."""
    for pause in (*BACKOFF_S, None):
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.TransportError as e:
            retryable = isinstance(e, httpx.ConnectError) or method == "GET"
            if pause is None or not retryable:
                raise ApiError(0, type(e).__name__) from None
            await asyncio.sleep(pause)
            continue
        if response.status_code in RETRY_STATUS and pause is not None and method == "GET":
            await asyncio.sleep(pause)
            continue
        if response.is_success:
            return response
        raise ApiError(response.status_code, _detail(response), _retry_after(response))
    raise AssertionError("unreachable")
