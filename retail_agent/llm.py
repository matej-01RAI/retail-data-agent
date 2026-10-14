"""Model construction: Gemini with retries, a fallback chain and a request-rate limiter.

    RateLimitedModel                 client-side RPM budget (free tier ~10-15 RPM)
      └─ FallbackModel               next model on API errors (429/5xx/timeouts)
           ├─ FaultInjectingModel    (demo/testing only: simulated outages)
           │    └─ GoogleModel       primary, HTTP retries w/ backoff in the SDK
           └─ GoogleModel            fallback (different model = separate quota)

Retries handle blips; the fallback handles a model being down or throttled;
the limiter keeps a multi-step turn from tripping the per-minute quota in the
first place. If everything fails, the turn ends with a friendly message and
the conversation stays intact (assistant.py).
"""

from __future__ import annotations

import asyncio
import collections
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from .settings import Settings


class RateLimitedModel(WrapperModel):
    """Sliding-window limiter: at most `rpm` requests in any 60 s window."""

    def __init__(self, wrapped: Model, rpm: int):
        super().__init__(wrapped)
        self.rpm = rpm
        self._stamps: collections.deque[float] = collections.deque()
        self._lock = asyncio.Lock()

    async def _acquire(self) -> None:
        if self.rpm <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= 60:
                    self._stamps.popleft()
                if len(self._stamps) < self.rpm:
                    self._stamps.append(now)
                    return
                await asyncio.sleep(60 - (now - self._stamps[0]) + 0.05)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        await self._acquire()
        return await self.wrapped.request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        await self._acquire()
        async with self.wrapped.request_stream(*args, **kwargs) as stream:
            yield stream


class FaultInjectingModel(WrapperModel):
    """Wraps the primary model: counts the requests it served (so a turn knows whether the
    fallback answered) and can fail the next `fail_next` requests with HTTP 503 to demo
    and test the fallback path."""

    def __init__(self, wrapped: Model):
        super().__init__(wrapped)
        self.fail_next = 0
        self.served = 0

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ModelHTTPError(503, self.model_name, {"error": "Simulated outage (fault injection)"})
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        self.served += 1
        return response


def build_model(settings: Settings) -> tuple[Model, FaultInjectingModel]:
    """Returns (model to give the agent, handle used to inject primary-model faults)."""
    from google.genai.types import HttpRetryOptions
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.providers.google import GoogleProvider

    if not settings.gemini_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey "
            "and put it in .env (see .env.example)."
        )
    provider = GoogleProvider(
        api_key=settings.gemini_api_key,
        retry_options=HttpRetryOptions(
            attempts=3,
            initial_delay=2.0,
            max_delay=20.0,
            exp_base=2.0,
            jitter=1.0,
            http_status_codes=[408, 429, 500, 502, 503, 504],
        ),
    )
    primary = FaultInjectingModel(GoogleModel(settings.model, provider=provider))
    chain: Model = primary
    if settings.fallback_models:
        chain = FallbackModel(primary, *[GoogleModel(name, provider=provider) for name in settings.fallback_models])
    return RateLimitedModel(chain, settings.llm_requests_per_minute), primary
