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
import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryCitation, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    BuiltinRuntime,
    CommitConnectorCheckpoint,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.errors import SourceConflictError
from powercontext.sources import (
    ConnectorBinding,
    SourceDefinitionRegistry,
    SourceObservation,
    SourceRef,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.connectors import ConnectorLifecycle, ConnectorRunResult
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def _write(path: Path, records: list[dict[str, object]]) -> None:
    header = {"type": "session", "version": 3, "id": "durable-session"}
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in [header, *records]) + "\n", encoding="utf-8"
    )


def _message(event_id: str, parent_id: str | None, text: str) -> dict[str, object]:
    return {
        "type": "message",
        "id": event_id,
        "parentId": parent_id,
        "message": {"role": "assistant", "content": text},
    }


class _RuntimeSink:
    def __init__(self, runtime: BuiltinRuntime, *, fail_event_id: str | None = None) -> None:
        self.runtime = runtime
        self.registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        self.fail_event_id = fail_event_id

    async def submit(
        self, binding: ConnectorBinding, item_id: str, definition_name: str, value: object, /
    ) -> SourceRef:
        if getattr(value, "event_id", None) == self.fail_event_id:
            raise OSError("simulated persistent storage failure")  # noqa: TRY003
        source = await self.registry.resolve(value)
        receipt = await self.runtime.ingestion.submit(
            SubmitSourceObservation(
                scope_id=binding.scope_id, observation=project_source_for_transport(self.registry, source)
            )
        )
        return receipt.source_ref


class _RuntimeCheckpoints:
    def __init__(self, runtime: BuiltinRuntime) -> None:
        self.runtime = runtime

    async def load(self, binding: ConnectorBinding, /) -> JsonValue | None:
        return (await self.runtime.ingestion.checkpoint(binding)).checkpoint

    async def save(
        self, binding: ConnectorBinding, checkpoint: JsonValue | None, /, *, expected: JsonValue | None
    ) -> None:
        await self.runtime.ingestion.commit(
            CommitConnectorCheckpoint(binding=binding, checkpoint=checkpoint, expected=expected)
        )


class _ChunkedEvidencePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            assert isinstance(source, SourceObservation)
            evidence = TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
            # Only derived Memory entries are bounded; their exact Source remains complete.
            for index in range(0, len(evidence.content), 1500):
                entries.append(
                    MemoryEntryInput(kind="pi-history", text=evidence.content[index : index + 1500], sources=(source,))
                )
        return tuple(entries)


def test_branch_replay_restart_and_failed_ingestion_preserve_durable_progress(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "session.jsonl"
        records: list[dict[str, object]] = [
            _message("root", None, "shared evidence"),
            _message("left", "root", "left evidence"),
            {"type": "model_change", "id": "control", "parentId": "root"},
            _message("right", "control", "right evidence"),
        ]
        _write(path, records)
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'history.db'}"))
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Pi history", summary="Durable branch evidence", idempotency_key="pi-history")
            )
            await runtime.ingestion.register(manifest_for_definition(PI_EVENT_SOURCE_DEFINITION))
        binding = ConnectorBinding(
            scope_id=scope.scope_id,
            binding_id="pi-history",
            connector_name="pi-session-history",
            connector_version="1",
        )

        async def run(leaf: str, *, fail: str | None = None) -> ConnectorRunResult:
            # Each call creates a new Runtime and Connector, exercising actual persisted recovery.
            async with open_builtin_runtime(config) as runtime:
                lifecycle = ConnectorLifecycle(
                    sink=_RuntimeSink(runtime, fail_event_id=fail), checkpoints=_RuntimeCheckpoints(runtime)
                )
                return await lifecycle.run(PiSessionConnector(path, leaf), binding)

        async def contents() -> list[str]:
            async with open_builtin_contexts(config) as contexts:
                entries = await (await contexts.get(scope.scope_id)).sources.journal.entries()
                return [
                    TextEvidence.model_validate(entry.source.projection(TEXT_EVIDENCE_PROJECTION_KEY)).content
                    for entry in entries
                    if isinstance(entry.source, SourceObservation)
                ]

        first = await run("left")
        assert len(first.items) == 2
        switched = await run("right")
        assert len(switched.items) == 1
        assert switched.previous_checkpoint == first.committed_checkpoint
        assert await contents() == [
            "assistant: shared evidence",
            "assistant: left evidence",
            "assistant: right evidence",
        ]
        replay = await run("right")
        assert replay.items == ()
        assert replay.committed_checkpoint == switched.committed_checkpoint

        records.extend([
            _message("append-1", "right", "first append"),
            _message("append-2", "append-1", "second append"),
        ])
        _write(path, records)
        with pytest.raises(OSError, match="storage failure"):
            await run("append-2", fail="append-2")
        async with open_builtin_runtime(config) as runtime:
            assert (await runtime.ingestion.checkpoint(binding)).checkpoint == switched.committed_checkpoint
        assert (await contents())[-1] == "assistant: first append"
        recovered = await run("append-2")
        assert recovered.previous_checkpoint == switched.committed_checkpoint
        assert await contents() == [
            "assistant: shared evidence",
            "assistant: left evidence",
            "assistant: right evidence",
            "assistant: first append",
            "assistant: second append",
        ]

        path.write_bytes(path.read_bytes() + b"{bad json}\n")
        with pytest.raises(ValueError):
            await run("append-2")
        _write(path, records)
        repaired = await run("append-2")
        assert repaired.items == ()
        assert repaired.previous_checkpoint == recovered.committed_checkpoint

        original = records[1]
        records[1] = _message("left", "root", "rewritten evidence on an unselected branch")
        _write(path, records)
        with pytest.raises(ValueError):
            await run("append-2")
        records[1] = original
        _write(path, records)
        stable = await run("append-2")
        assert stable.items == ()
        assert stable.committed_checkpoint == recovered.committed_checkpoint
        assert "assistant: left evidence" in await contents()

    asyncio.run(scenario())


def test_every_event_and_large_tool_result_can_be_read_through_exact_memory_citations(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "large.jsonl"
        large_output = "toolneedle 中文完整证据🙂\n" * 1000 + "tailneedle 完整末尾"
        records: list[dict[str, object]] = [
            _message("first", None, "firstneedle 原始文本"),
            _message("second", "first", "secondneedle 第二条相同角色"),
            {
                "type": "message",
                "id": "tool-result",
                "parentId": "second",
                "message": {
                    "role": "toolResult",
                    "toolName": "read",
                    "toolCallId": "call-1",
                    "content": [{"type": "text", "text": large_output}],
                    "retained": {"status": 0},
                },
            },
        ]
        _write(path, records)
        events = parse_pi_session(PiSessionInput(content=path.read_bytes()))
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        observations = [project_source_for_transport(registry, await registry.resolve(event)) for event in events]
        expected = dict(zip((source.name for source in observations), events, strict=True))
        assert len(events[-1].content.encode()) > 8192
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'evidence.db'}"))
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Original events", summary="Exact evidence references", idempotency_key="pi-evidence")
            )
            await runtime.ingestion.register(manifest_for_definition(PI_EVENT_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            conflicting_event = parse_pi_session(
                PiSessionInput(
                    content=(
                        json.dumps({"type": "session", "version": 3, "id": "durable-session"})
                        + "\n"
                        + json.dumps(_message("first", None, "rewritten"))
                    ).encode()
                )
            )[0]
            changed = project_source_for_transport(registry, await registry.resolve(conflicting_event))
            assert changed.name == observations[0].name
            with pytest.raises(SourceConflictError):
                await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=changed))

        async with open_builtin_runtime(config, candidate_pipeline=_ChunkedEvidencePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            for _ in range(len(events) + 1):
                if not (await memory.flush()).processed:
                    break
            else:
                pytest.fail("Pi Source ingestion did not drain")

        async with open_builtin_contexts(config) as contexts:
            context = await contexts.get(scope.scope_id)
            entries = await context.sources.journal.entries()
            stored = {entry.source_ref.source_id: entry.source for entry in entries}
            assert len(entries) == len(events)
            for identity, event in expected.items():
                observation = stored[identity]
                assert isinstance(observation, SourceObservation)
                assert await context.sources.get(observation) == observation
                reconstructed = PI_EVENT_SOURCE_DEFINITION.source_class.model_validate(observation.payload)
                assert await registry.read(reconstructed) == event
                assert TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)).content == (
                    event.content
                )

        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            cited = set()
            for needle, event in zip(("firstneedle", "secondneedle", "tailneedle"), events, strict=True):
                page = await memory.search(SearchMemoryRequest(query=needle, mode="fts", limit=100))
                assert page.hits
                matching = set()
                for hit in page.hits:
                    entry = await memory.get(
                        GetMemoryEntryRequest(
                            citation=MemoryCitation(
                                memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                            )
                        )
                    )
                    for reference in entry.entry.sources:
                        original = expected[reference.source_id]
                        assert original == event
                        matching.add(reference.source_id)
                        cited.add(reference.source_id)
                assert matching == {(await registry.resolve(event)).name}
            assert cited == set(expected)

    asyncio.run(scenario())
