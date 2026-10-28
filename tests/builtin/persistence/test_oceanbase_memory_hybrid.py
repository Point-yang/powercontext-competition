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

from __future__ import annotations

import asyncio
import math
import os
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemorySearchRequest,
)
from powercontext.builtin.persistence.oceanbase.memory_index import OceanBaseMemoryHybridIndex
from powercontext.builtin.tags import TagFilter


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(profile_id="test", model="test", dimension=3, distance="l2", normalization="unit")


def _request(*, mode="hybrid", tag_filter=None) -> MemorySearchRequest:
    return MemorySearchRequest(
        query="lease recovery",
        analyzed_query="lease recovery",
        memories=(ArtifactRef(family="memory", artifact_id="memory-a", revision=9),),
        candidate_limit=7,
        mode=mode,
        query_vector=(1.0, 0.0, 0.0),
        embedding_profile=_profile(),
        tag_filter=tag_filter,
    )


@pytest.mark.parametrize("tags", [None, TagFilter(tags=("selected", "reviewed"), match="all")])
def test_hybrid_candidate_budget_and_current_head_citations(tags) -> None:
    async def scenario() -> None:
        """One candidate statement preserves both channels and old active projections."""
        index = OceanBaseMemoryHybridIndex(_profile())
        index.vector_complete = AsyncMock(return_value=True)
        row = {
            "memory_artifact_id": "memory-a",
            "head_revision": 1,
            "entry_id": "entry-a",
            "entry_version_id": "version-a",
            "text": "Original exact body\nwith unicode: 恢复",
            "distance": None,
        }
        connection = AsyncMock(spec=AsyncConnection)
        connection.execute.return_value.mappings = MagicMock()
        connection.execute.return_value.mappings.return_value.all.return_value = [
            {**row, "channel": "fts"},
            {**row, "channel": "vector", "distance": 0.25},
        ]

        result = await index.search(cast(AsyncConnection, connection), "scope-a", _request(tag_filter=tags))

        assert connection.execute.await_count == 1
        assert result.fts[0].memory_ref.revision == 9
        assert result.vector[0].memory_ref == result.fts[0].memory_ref
        assert result.vector[0].entry_version_id == "version-a"
        assert result.vector[0].distance == 0.25
        assert result.fts[0].text == row["text"]
        statement, parameters = connection.execute.await_args.args
        compiled = str(statement.compile(dialect=mysql.dialect()))
        assert "MATCH(f.searchable_text) AGAINST" in compiled
        assert "ORDER BY l2_distance(m.embedding" in compiled
        assert "APPROXIMATE" in compiled
        assert compiled.count("LIMIT") == 2
        assert "f.head_revision =" not in compiled
        assert "m.head_revision =" not in compiled
        assert parameters["scope_id"] == "scope-a"
        assert parameters["memory_artifact_ids"] == ("memory-a",)
        assert parameters["candidate_limit"] == 7
        if tags:
            assert compiled.count("tags.tag_key IN") == 2
            assert compiled.index("tags.tag_key IN") < compiled.index("LIMIT")
            assert parameters["tag_minimum"] == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["fts", "vector"])
def test_single_channel_search_uses_its_existing_strategy(mode) -> None:
    async def scenario() -> None:
        index = OceanBaseMemoryHybridIndex(_profile())
        index._vector.vector_complete = AsyncMock(return_value=True)
        connection = AsyncMock(spec=AsyncConnection)
        connection.execute.return_value.mappings = MagicMock(return_value=())
        channels = await index.search(cast(AsyncConnection, connection), "scope-a", _request(mode=mode))
        assert channels.fts == ()
        assert channels.vector == ()
        assert connection.execute.await_count == 1
        statement = str(connection.execute.await_args.args[0].compile(dialect=mysql.dialect()))
        assert ("MATCH" in statement) == (mode == "fts")
        assert ("l2_distance" in statement) == (mode == "vector")

    asyncio.run(scenario())


def test_hybrid_rejects_incomplete_embeddings_before_candidate_access() -> None:
    async def scenario() -> None:
        index = OceanBaseMemoryHybridIndex(_profile())
        index.vector_complete = AsyncMock(return_value=False)
        connection = AsyncMock(spec=AsyncConnection)
        with pytest.raises(CapabilityNotSupportedError):
            await index.search(cast(AsyncConnection, connection), "scope-a", _request())
        connection.execute.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.skipif(os.environ.get("POWERCONTEXT_TEST_SEEKDB") != "1", reason="requires POWERCONTEXT_TEST_SEEKDB=1")
def test_native_hybrid_applies_sparse_tags_before_ann_limit(tmp_path) -> None:
    """The sole tagged neighbor can lie beyond the unfiltered ANN top-k."""
    pytest.importorskip("pylibseekdb")
    from sqlalchemy import Engine, event

    from powercontext.builtin.artifacts.memory import MemoryEntryInput
    from powercontext.builtin.inference import EmbeddingResult
    from powercontext.builtin.persistence.seekdb import SeekDBConfig
    from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
    from powercontext.builtin.tags import MemoryEntryTagTarget

    class Embedding:
        profile = _profile()

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            angles = tuple(
                int(value.split("ordinal ")[1].rstrip(".")) / 200 if "ordinal " in value else 0 for value in texts
            )
            return EmbeddingResult(vectors=tuple((math.cos(angle), math.sin(angle), 0.0) for angle in angles))

    async def scenario() -> None:
        statements = []

        def record(_connection, _cursor, sql, parameters, _context, _executemany) -> None:
            if "WITH fts_candidates" in sql:
                statements.append((sql, parameters))

        async with open_builtin_contexts(
            BuiltinConfig(database=SeekDBConfig(path=tmp_path / "db")), embedding_model=Embedding()
        ) as contexts:
            memory = (await contexts.get("project")).artifacts.memory
            head = await memory.remember(
                memory=None,
                entries=tuple(
                    MemoryEntryInput(kind="fact", text=f"Vector evidence ordinal {index}.") for index in range(64)
                ),
                mode="append",
            )
            assert head is not None
            selected = next(
                entry for entry in await memory.entries(head) if entry.text == "Vector evidence ordinal 63."
            )
            target = MemoryEntryTagTarget(artifact_id=head.artifact_id, entry_id=selected.entry_id)
            tags = await contexts.records.get_tags("project", target)
            await contexts.records.replace_tags("project", target, ("selected", "reviewed"), expected_etag=tags.etag)
            event.listen(Engine, "before_cursor_execute", record)
            try:
                result = await memory.search(
                    "unseen-query",
                    memories=(head,),
                    mode="hybrid",
                    limit=1,
                    tag_filter=TagFilter(tags=("selected", "reviewed"), match="all"),
                )
            finally:
                event.remove(Engine, "before_cursor_execute", record)
            assert len(statements) == 1
            assert [hit.entry_id for hit in result.hits] == [selected.entry_id]
            assert result.hits[0].entry_version_id == selected.entry_version_id
            assert result.hits[0].text == selected.text
            assert result.hits[0].matched_by == ("vector",)
            assert result.hits[0].memory_ref == head.as_ref()
            missing = await memory.search(
                "unseen-query", memories=(head,), mode="hybrid", limit=1, tag_filter=TagFilter(tags=("missing",))
            )
            assert missing.hits == ()
            sql, parameters = statements[0]
            async with contexts.database.transaction() as connection:
                plan = "\n".join(
                    str(row[0]) for row in (await connection.exec_driver_sql("EXPLAIN " + sql, parameters)).all()
                )
            assert "TEXT RETRIEVAL SCAN" in plan
            assert "VECTOR INDEX" in plan

    asyncio.run(scenario())
