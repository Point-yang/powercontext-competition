# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Single-loop embedding request sharing and bounded successful-result caching."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel, QueryEmbeddingModel


@dataclass(frozen=True, slots=True)
class _BatchUsage:
    sequence: int
    requests: int
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class _Completed:
    vector: EmbeddingVector
    batch: _BatchUsage


# Query/document roles cannot share a vector when the provider is asymmetric.
_Key = tuple[bool, str]


class CoalescingEmbeddingModel:
    """Merge overlapping calls without coupling cancellation or usage attribution.

    Each pending text owns a Future until its provider batch settles. Callers shield
    those Futures, so cancelling one caller never cancels work used by another.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)):
            raise ValueError("coalesce_ms must be finite and non-negative")  # noqa: TRY003, TRY004
        if not math.isfinite(coalesce_ms) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be finite and non-negative")  # noqa: TRY003
        self._model = model
        self.profile = model.profile
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[_Key, EmbeddingVector] = OrderedDict()
        self._pending: dict[_Key, asyncio.Future[_Completed]] = {}
        self._queued: dict[_Key, asyncio.Future[_Completed]] = {}
        self._dispatch_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._sequence = 0

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed documents, restoring the caller's original duplicate positions."""

        return await self._embed(texts, query=False)

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Preserve query-specific providers; symmetric queries share document work."""

        return await self._embed(texts, query=isinstance(self._model, QueryEmbeddingModel))

    async def _embed(self, texts: tuple[str, ...], *, query: bool) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        if any(not isinstance(text, str) for text in texts):
            raise ValueError("embedding inputs must be strings")  # noqa: TRY003
        loop = asyncio.get_running_loop()
        vectors: dict[str, EmbeddingVector] = {}
        waiting: dict[str, asyncio.Future[_Completed]] = {}
        for text in texts:
            key = (query, text)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                vectors[text] = cached
                continue
            future = self._pending.get(key)
            if future is None:
                future = loop.create_future()
                # A cancelled sole waiter must not leave unobserved provider errors.
                future.add_done_callback(_observe_failure)
                self._pending[key] = future
                self._queued[key] = future
            waiting[text] = future
        if self._queued and self._dispatch_task is None:
            self._dispatch_task = asyncio.create_task(self._dispatch())
        completed = await asyncio.gather(*(asyncio.shield(future) for future in waiting.values()))
        batches: dict[int, _BatchUsage] = {}
        for text, item in zip(waiting, completed, strict=True):
            vectors[text] = item.vector
            batches[item.batch.sequence] = item.batch
        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in texts),
            usage=InferenceUsage(
                requests=sum(batch.requests for batch in batches.values()),
                input_tokens=_sum_tokens(tuple(batch.input_tokens for batch in batches.values())),
                output_tokens=_sum_tokens(tuple(batch.output_tokens for batch in batches.values())),
            ),
        )

    async def _dispatch(self) -> None:
        try:
            await asyncio.sleep(self._coalesce_seconds)
            queued, self._queued = self._queued, {}
            for query in (False, True):
                items = tuple((key, future) for key, future in queued.items() if key[0] is query)
                for offset in range(0, len(items), self._max_batch_size):
                    batch = items[offset : offset + self._max_batch_size]
                    task = asyncio.create_task(self._run_batch(batch, query=query))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        finally:
            self._dispatch_task = None

    async def _run_batch(
        self,
        batch: tuple[tuple[_Key, asyncio.Future[_Completed]], ...],
        *,
        query: bool,
    ) -> None:
        try:
            texts = tuple(key[1] for key, _ in batch)
            if query and isinstance(self._model, QueryEmbeddingModel):
                result = await self._model.embed_query(texts)
            else:
                result = await self._model.embed(texts)
            _validate_vector_count(result, count=len(texts))
            self._sequence += 1
            usage = _BatchUsage(
                sequence=self._sequence,
                requests=result.usage.requests,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            )
            for (key, future), vector in zip(batch, result.vectors, strict=True):
                # Retain immutable vectors and detached usage, never provider result objects.
                frozen = tuple(vector)
                if self._cache_size:
                    self._cache[key] = frozen
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)
                future.set_result(_Completed(vector=frozen, batch=usage))
        except (Exception, asyncio.CancelledError) as exc:
            for _, future in batch:
                if not future.done():
                    future.set_exception(exc)
        finally:
            for key, future in batch:
                if self._pending.get(key) is future:
                    del self._pending[key]


def _observe_failure(future: asyncio.Future[_Completed]) -> None:
    if not future.cancelled():
        future.exception()


def _sum_tokens(values: tuple[int | None, ...]) -> int | None:
    # A partial provider count is not a complete total. Match the Runtime's
    # portable usage aggregation instead of silently interpreting unknown as zero.
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _validate_vector_count(result: EmbeddingResult, *, count: int) -> None:
    if len(result.vectors) != count:
        raise ValueError("provider vector count does not match the batch")  # noqa: TRY003


__all__ = ["CoalescingEmbeddingModel"]
