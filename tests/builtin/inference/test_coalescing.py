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

"""Sharing, failure isolation and provider identity at the embedding boundary."""

import asyncio

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts


class _Provider:
    profile = EmbeddingProfile(profile_id="coalescing-tests", model="offline", dimension=1)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
        self.calls.append(texts)
        return EmbeddingResult(
            vectors=tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=2, input_tokens=7, output_tokens=1),
        )


@pytest.mark.anyio
async def test_late_waiter_shares_inflight_work_even_without_cache() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class Blocked(_Provider):
        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
            started.set()
            await release.wait()
            return await super().embed(texts)

    provider = Blocked()
    model = CoalescingEmbeddingModel(provider, coalesce_ms=0, cache_size=0)
    first = asyncio.create_task(model.embed(("same", "same")))
    await started.wait()
    second = asyncio.create_task(model.embed(("same",)))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    result = await second
    assert result.vectors == ((4.0,),)
    assert result.usage == InferenceUsage(requests=2, input_tokens=7, output_tokens=1)
    assert provider.calls == [("same",)]
    await model.embed(("same",))
    assert provider.calls == [("same",), ("same",)]


@pytest.mark.anyio
async def test_partial_batch_failure_does_not_poison_independent_work_or_cache() -> None:
    class Flaky(_Provider):
        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
            self.calls.append(texts)
            if "bad" in texts and sum("bad" in call for call in self.calls) == 1:
                raise RuntimeError("temporary provider failure")  # noqa: TRY003
            return EmbeddingResult(vectors=tuple((float(len(text)),) for text in texts))

    provider = Flaky()
    model = CoalescingEmbeddingModel(provider, max_batch_size=1, coalesce_ms=0)
    failed, success = await asyncio.gather(model.embed(("bad",)), model.embed(("good",)), return_exceptions=True)
    assert isinstance(failed, RuntimeError)
    assert isinstance(success, EmbeddingResult) and success.vectors == ((4.0,),)
    assert (await model.embed(("good",))).usage.requests == 0
    assert (await model.embed(("bad",))).vectors == ((3.0,),)
    assert provider.calls.count(("bad",)) == 2
    assert provider.calls.count(("good",)) == 1


@pytest.mark.anyio
async def test_usage_sums_each_waited_batch_once_and_detaches_mutable_results() -> None:
    provider = _Provider()
    model = CoalescingEmbeddingModel(provider, max_batch_size=2, coalesce_ms=0)
    first, second = await asyncio.gather(model.embed(("a", "bb", "a", "ccc")), model.embed(("bb", "ccc")))
    assert first.vectors == ((1.0,), (2.0,), (1.0,), (3.0,))
    assert first.usage == InferenceUsage(requests=4, input_tokens=14, output_tokens=2)
    assert second.usage == InferenceUsage(requests=4, input_tokens=14, output_tokens=2)
    first.vectors = ((900.0,),)
    first.usage.requests = 900
    cached = await model.embed(("a", "bb"))
    assert cached.vectors == ((1.0,), (2.0,))
    assert cached.usage.requests == 0


@pytest.mark.anyio
async def test_usage_preserves_unknown_provider_token_counts_instead_of_publishing_a_partial_total() -> None:
    class PartiallyMetered(_Provider):
        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
            return EmbeddingResult(
                vectors=tuple((1.0,) for _ in texts),
                usage=InferenceUsage(
                    requests=2,
                    input_tokens=7 if texts == ("known",) else None,
                    output_tokens=0,
                ),
            )

    model = CoalescingEmbeddingModel(PartiallyMetered(), max_batch_size=1, coalesce_ms=0)
    result = await model.embed(("known", "unknown", "known"))
    assert result.usage == InferenceUsage(requests=4, input_tokens=None, output_tokens=0)
    assert (await model.embed(("known", "unknown"))).usage == InferenceUsage(requests=0)
    assert (await model.embed(())).usage == InferenceUsage(requests=0)


@pytest.mark.anyio
async def test_asymmetric_query_and_document_vectors_do_not_share_cache() -> None:
    class Asymmetric(_Provider):
        async def embed_query(self, texts: tuple[str, ...]) -> EmbeddingResult:
            self.calls.append(texts)
            return EmbeddingResult(vectors=tuple((-1.0,) for _ in texts))

    provider = Asymmetric()
    model = CoalescingEmbeddingModel(provider, coalesce_ms=0)
    assert (await model.embed(("same",))).vectors == ((4.0,),)
    assert (await model.embed_query(("same",))).vectors == ((-1.0,),)
    assert (await model.embed(("same",))).vectors == ((4.0,),)
    assert len(provider.calls) == 2


@pytest.mark.parametrize("vectors", [(), ((1.0,), (2.0,))])
@pytest.mark.anyio
async def test_invalid_provider_results_can_be_retried_without_cache_pollution(vectors) -> None:
    class InvalidOnce(_Provider):
        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
            self.calls.append(texts)
            return EmbeddingResult(vectors=vectors if len(self.calls) == 1 else ((1.0,),))

    provider = InvalidOnce()
    model = CoalescingEmbeddingModel(provider, coalesce_ms=0)
    with pytest.raises(ValueError):
        await model.embed(("text",))
    assert (await model.embed(("text",))).vectors == ((1.0,),)
    assert len(provider.calls) == 2


@pytest.mark.anyio
async def test_wrapper_preserves_successful_provider_vectors_without_reinterpreting_the_profile() -> None:
    class ProviderManaged(_Provider):
        # The wrapper coordinates requests. Dimension enforcement belongs to the
        # Memory/index boundary, not to the provider's generic batch result.
        profile = EmbeddingProfile(profile_id="provider-managed", model="offline", dimension=3)

    provider = ProviderManaged()
    model = CoalescingEmbeddingModel(provider, coalesce_ms=0)
    first = await model.embed(("four", "eight", "four"))
    assert first.vectors == ((4.0,), (5.0,), (4.0,))
    assert model.profile is provider.profile
    cached = await model.embed(("eight", "four"))
    assert cached.vectors == ((5.0,), (4.0,))
    assert cached.usage.requests == 0
    assert provider.calls == [("four", "eight")]


@pytest.mark.parametrize("vector", [(1.0, 2.0), (float("nan"),)])
@pytest.mark.anyio
async def test_native_memory_keeps_dimension_and_finite_validation_at_its_own_boundary(vector) -> None:
    class InvalidCoordinates(_Provider):
        async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
            return EmbeddingResult(vectors=tuple(vector for _ in texts))

    model = CoalescingEmbeddingModel(InvalidCoordinates(), coalesce_ms=0)
    async with open_builtin_contexts(BuiltinConfig(database=SQLiteConfig()), embedding_model=model) as contexts:
        service = (await contexts.get("invalid-embedding-coordinates")).artifacts.memory
        with pytest.raises(ValueError, match=r"embedding must contain exactly|embedding values must all be finite"):
            await service.remember(
                memory=None,
                entries=(MemoryEntryInput(kind="fact", text="Memory enforces its vector profile."),),
                mode="append",
            )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_batch_size": 0},
        {"max_batch_size": True},
        {"max_batch_size": 1.5},
        {"cache_size": -1},
        {"cache_size": True},
        {"coalesce_ms": -1},
        {"coalesce_ms": float("inf")},
        {"coalesce_ms": float("nan")},
    ],
)
def test_rejects_invalid_coalescing_configuration(kwargs) -> None:
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(_Provider(), **kwargs)
