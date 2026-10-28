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

"""Lifecycle recovery at the real Memory persistence boundary."""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft


def _event(available: bool, sequence: int = 1) -> SourceLifecycleEvent:
    return SourceLifecycleEvent(node_id="root", revision=7, sequence=sequence, available=available, reason="fixture")


@pytest.mark.parametrize(
    "field,value",
    [(field, value) for field in ("revision", "sequence") for value in (True, False, 0, -1, 1.0, "1")]
    + [("node_id", " "), ("reason", ""), ("available", 1), ("available", "false")],
)
def test_lifecycle_events_reject_coercions(field, value):
    values = _event(False).model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        SourceLifecycleEvent(**values)


async def test_lifecycle_retry_after_native_write_failure_and_reopen(tmp_path):
    database = tmp_path / "lifecycle-recovery.db"
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
    async with open_builtin_runtime(config) as runtime:
        assert runtime.scopes is not None
        scope = (
            await runtime.scopes.create(ScopeDraft(title="Recovery", summary="Evidence", idempotency_key="a"))
        ).scope_id
        receipt = await runtime.sources.for_scope(scope).capture(
            CaptureSource(source_id="root", content="evidence", metadata={})
        )
        memory = runtime.memory.for_scope(scope)
        result = await memory.remember(RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="retained"),)))
        assert result.entry is not None
        citation = result.entry.citation
        lifecycle = runtime.lifecycle.for_scope(scope)
        await lifecycle.register_source("root", receipt.source_ref, revision=7)
        await lifecycle.register_memory("derived", citation, parents=("root",), policy="all")
        before = (await memory.list()).memory_ref
        await lifecycle.register_memory("derived", citation, parents=("root",), policy="all")
        assert (await memory.list()).memory_ref == before
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TRIGGER fail_lifecycle BEFORE INSERT ON pc_artifacts "
                "WHEN NEW.family='memory' BEGIN SELECT RAISE(ABORT, 'controlled state write failure'); END"
            )
        with pytest.raises(Exception, match="controlled state write failure"):
            await lifecycle.apply(_event(False))
        assert (await memory.list()).memory_ref == before
        with pytest.raises(SourceLifecycleError):
            await lifecycle.resolve("derived", purpose="generation")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER fail_lifecycle")
    async with open_builtin_runtime(config) as runtime:
        lifecycle = runtime.lifecycle.for_scope(scope)
        memory = runtime.memory.for_scope(scope)
        assert await lifecycle.apply(_event(False)) == []
        assert not (await memory.list()).entries
        withdrawn = (await memory.list(include_inactive=True)).memory_ref
        await lifecycle.apply(_event(False))
        assert (await memory.list(include_inactive=True)).memory_ref == withdrawn
        assert await lifecycle.resolve("derived", purpose="audit") == citation
        assert (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text == "retained"
        assert await lifecycle.apply(_event(True, 2)) == ["derived", "root"]
        assert (await memory.list()).entries[0].entry.entry_version_id == citation.entry_version_id
        restored = (await memory.list()).memory_ref
        await lifecycle.apply(_event(True, 2))
        assert (await memory.list()).memory_ref == restored


async def test_lifecycle_scope_isolation_and_readable_reference_validation():
    async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
        assert runtime.scopes is not None
        scopes = []
        for key in ("left", "right"):
            scope = (
                await runtime.scopes.create(ScopeDraft(title=key, summary="evidence", idempotency_key=key))
            ).scope_id
            receipt = await runtime.sources.for_scope(scope).capture(
                CaptureSource(source_id="same", content=key, metadata={})
            )
            lifecycle = runtime.lifecycle.for_scope(scope)
            await lifecycle.register_source("root", receipt.source_ref, revision=7)
            await lifecycle.register_source("root", receipt.source_ref, revision=7)
            scopes.append((lifecycle, receipt.source_ref))
        left, right = scopes
        await left[0].apply(_event(False))
        with pytest.raises(SourceLifecycleError):
            await left[0].resolve("root", purpose="generation")
        assert await right[0].resolve("root", purpose="generation") == right[1]
        with pytest.raises(SourceLifecycleError):
            await right[0].register_source("root", right[1], revision=8)
        with pytest.raises(SourceLifecycleError):
            await right[0].resolve("root", purpose="unknown")
