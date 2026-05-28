from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
import secrets
from typing import Annotated, Callable

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse

from src.api.dependencies import ApiSettings


TOKEN_HEADER = "X-Mood-Token"
READS_SAME_HOST_ONLY = {"detail": "Reads same-host only"}


def is_same_host_client(client_host: str | None, lan_bind_ip: str) -> bool:
    return client_host in {"127.0.0.1", "::1", lan_bind_ip}


def token_matches(provided_token: str | None, expected_token: str) -> bool:
    return secrets.compare_digest(provided_token or "", expected_token)


def build_require_token(settings_provider: Callable[[], ApiSettings]):
    async def require_token(
        x_mood_token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
    ) -> None:
        settings = settings_provider()
        if not token_matches(x_mood_token, settings.mood_token):
            raise HTTPException(status_code=401, detail="Invalid token")

    return require_token


def build_same_host_read_middleware(settings_provider: Callable[[], ApiSettings]):
    async def restrict_reads_to_same_host(request: Request, call_next):
        if request.method == "GET" and request.url.path.startswith("/api/"):
            settings = settings_provider()
            client_host = request.client.host if request.client is not None else None
            if not is_same_host_client(client_host, settings.lan_bind_ip):
                return JSONResponse(status_code=403, content=READS_SAME_HOST_ONLY)
        return await call_next(request)

    return restrict_reads_to_same_host


class InMemoryRateLimiter:
    def __init__(
        self,
        *,
        limit: int = 10,
        window_seconds: int = 60,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._limit = limit
        self._window = timedelta(seconds=window_seconds)
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._events: dict[str, deque[datetime]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._now_provider().astimezone(UTC)
        threshold = now - self._window
        bucket = self._events[key]

        while bucket and bucket[0] <= threshold:
            bucket.popleft()

        if len(bucket) >= self._limit:
            return False

        bucket.append(now)
        return True


def enforce_post_rate_limit(request: Request, limiter: InMemoryRateLimiter) -> None:
    client_host = request.client.host if request.client is not None else "unknown"
    if not limiter.allow(client_host):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
