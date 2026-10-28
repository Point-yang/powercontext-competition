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

"""Behavior checks at the HTTP scoring boundary."""

import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def _hit(index: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="memory", revision=1),
        entry_id=f"entry-{index}",
        entry_version_id=f"version-{index}",
        text=f"document {index}",
        score=1.0,
        matched_by=("fts",),
    )


@pytest.mark.anyio
async def test_later_batch_winners_and_cross_batch_ties_use_global_ranks() -> None:
    scores = {1: -1.0, 2: 0.9, 3: 0.95, 4: 0.9, 5: 0.1}
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        records = body["candidates"]
        seen.extend(record["rank"] for record in records)
        assert all(record["text"] == _hit(record["rank"]).text for record in records)
        return httpx.Response(
            200, json={"scores": [{"rank": item["rank"], "score": scores[item["rank"]]} for item in reversed(records)]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HTTPMemoryReranker("https://scorer.invalid", client=client, batch_size=2).rerank(
            "query", tuple(_hit(index) for index in range(1, 6)), 4
        )
    assert result.selected_ranks == (3, 2, 4, 5)
    assert result.usage.requests == 3
    assert sorted(seen) == list(range(1, 6))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"scores": {}},
        {"scores": [None]},
        {"scores": [{"rank": True, "score": 1}]},
        {"scores": [{"rank": 1.0, "score": 1}]},
        {"scores": [{"rank": 1, "score": True}]},
        {"scores": [{"rank": 1, "score": "0.5"}]},
    ],
)
@pytest.mark.anyio
async def test_rejects_json_values_outside_the_scoring_schema(payload: object) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ValueError):
            await HTTPMemoryReranker("https://scorer.invalid", client=client).rerank("query", (_hit(1),), 1)


@pytest.mark.anyio
async def test_preserves_status_errors_and_reports_invalid_json_as_value_error() -> None:
    for response, expected_error in (
        (httpx.Response(503), httpx.HTTPStatusError),
        (httpx.Response(200, content=b"not JSON"), ValueError),
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _, response=response: response)) as client:
            with pytest.raises(expected_error):
                await HTTPMemoryReranker("https://scorer.invalid", client=client).rerank("query", (_hit(1),), 1)


@pytest.mark.anyio
async def test_integer_scores_keep_their_json_precision() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"scores": [{"rank": 1, "score": 2**55}, {"rank": 2, "score": 2**55 + 1}]}
            )
        )
    ) as client:
        result = await HTTPMemoryReranker("https://scorer.invalid", client=client).rerank(
            "query", (_hit(1), _hit(2)), 1
        )
    assert result.selected_ranks == (2,)
