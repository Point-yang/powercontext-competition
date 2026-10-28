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

"""Identity-preserving Memory reranking using a bounded HTTP scoring protocol."""

from __future__ import annotations

import math
from typing import cast

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    """Score every candidate, then select from the complete pool in stable order."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Send each global rank exactly once and preserve its original identity."""

        if not candidates or type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("limit must select between one and all candidates")  # noqa: TRY003
        scored: dict[int, int | float] = {}
        requests = 0
        for offset in range(0, len(candidates), self._batch_size):
            batch = candidates[offset : offset + self._batch_size]
            ranks = set(range(offset + 1, offset + len(batch) + 1))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": offset + index, "text": candidate.text}
                        for index, candidate in enumerate(batch, start=1)
                    ],
                },
            )
            requests += 1
            response.raise_for_status()
            scored.update(_validate_scores(response.json(), ranks))
        return MemoryRerankDecision(
            selected_ranks=tuple(sorted(scored, key=lambda rank: (-scored[rank], rank))[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _validate_scores(payload: object, expected_ranks: set[int]) -> dict[int, int | float]:
    if not isinstance(payload, dict) or payload.keys() != {"scores"}:
        raise ValueError("response must contain only a scores array")  # noqa: TRY003
    records = cast("dict[str, object]", payload)["scores"]
    if not isinstance(records, list):
        raise ValueError("response must contain only a scores array")  # noqa: TRY003, TRY004 - Protocol contract.
    scores: dict[int, int | float] = {}
    for item in records:
        if not isinstance(item, dict) or item.keys() != {"rank", "score"}:
            raise ValueError("score records must contain only rank and score")  # noqa: TRY003
        record = cast("dict[str, object]", item)
        rank, score = record["rank"], record["score"]
        if not isinstance(rank, int) or isinstance(rank, bool) or rank not in expected_ranks or rank in scores:
            raise ValueError("scores must cover every batch rank exactly once")  # noqa: TRY003
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ValueError("score must be a finite number")  # noqa: TRY003, TRY004 - Protocol contract.
        if isinstance(score, float) and not math.isfinite(score):
            raise ValueError("score must be a finite number")  # noqa: TRY003
        scores[rank] = score
    if scores.keys() != expected_ranks:
        raise ValueError("scores must cover every batch rank exactly once")  # noqa: TRY003
    return scores


__all__ = ["HTTPMemoryReranker"]
