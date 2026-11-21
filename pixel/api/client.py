"""One httpx client for the 5U API. The key goes into the `X-API-Key` header and nowhere else."""

import httpx

from pixel.config import Settings


class ApiError(Exception):
    def __init__(self, status: int, detail: str, retry_after: float | None = None):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


def make_client(settings: Settings) -> httpx.AsyncClient:
    """Base URL, key header, timeouts. Never logs headers."""
    raise NotImplementedError


async def request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
    """Sends one call; retries connection errors and 5xx with backoff; raises ApiError for the rest."""
    raise NotImplementedError
