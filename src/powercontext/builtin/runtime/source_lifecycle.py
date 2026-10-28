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

"""Durable evidence eligibility, preserving exact historical references."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select, update

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """Invalid lifecycle identity, dependency, event, or generation request."""


class SourceLifecycleEvent(BaseModel):
    """A strictly typed, replayable Source availability transition."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: str
    reason: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool

    @field_validator("node_id", "reason")
    @classmethod
    def require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("lifecycle identity and reason must be nonblank")  # noqa: TRY003
        return value


class _Binding(BaseModel):
    """Immutable graph registration, separate from mutable eligibility."""

    kind: Literal["source", "memory", "topic"]
    source: SourceRef | None = None
    memory: MemoryCitation | None = None
    topic: ArtifactRef | None = None
    revision: int | None = None
    parents: tuple[str, ...] = ()
    policy: Literal["all", "any"] = "all"

    def reference(self) -> SourceRef | MemoryCitation | ArtifactRef:
        reference = self.source if self.kind == "source" else self.memory if self.kind == "memory" else self.topic
        if reference is None:
            raise SourceLifecycleError("stored lifecycle reference is missing")  # noqa: TRY003
        return reference


class SourceLifecycleApplication:
    """Scope selection for the persistent, single-writer dependency graph."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase) -> None:
        self.runtime = runtime
        self.database = database
        self._locks: dict[str, asyncio.Lock] = {}

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self, scope_id, self._locks.setdefault(scope_id, asyncio.Lock()))


class ScopedSourceLifecycle:
    """An immutable DAG with monotonic Source events and recoverable propagation."""

    def __init__(self, application: SourceLifecycleApplication, scope_id: str, lock: asyncio.Lock) -> None:
        self._application = application
        self._scope = scope_id
        self._lock = lock

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")  # noqa: TRY003
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source registration requires a SourceRef")  # noqa: TRY003
        await self._register(node_id, _Binding(kind="source", source=source_ref, revision=revision))

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: Literal["all", "any"]
    ) -> None:
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory registration requires a MemoryCitation")  # noqa: TRY003
        await self._register(node_id, self._derivative("memory", citation, parents, policy))

    async def register_topic(
        self, node_id: str, artifact_ref: ArtifactRef, *, parents: tuple[str, ...], policy: Literal["all", "any"]
    ) -> None:
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("topic registration requires a Topic Memory ArtifactRef")  # noqa: TRY003
        await self._register(node_id, self._derivative("topic", artifact_ref, parents, policy))

    @staticmethod
    def _derivative(kind, reference, parents, policy) -> _Binding:
        if policy not in {"all", "any"}:
            raise SourceLifecycleError("policy must be all or any")  # noqa: TRY003
        if not parents or any(not isinstance(parent, str) or not parent.strip() for parent in parents):
            raise SourceLifecycleError("derived nodes require nonblank parents")  # noqa: TRY003
        if len(set(parents)) != len(parents):
            raise SourceLifecycleError("duplicate parents are not allowed")  # noqa: TRY003
        return _Binding(
            kind=kind,
            memory=reference if kind == "memory" else None,
            topic=reference if kind == "topic" else None,
            parents=tuple(parents),
            policy=policy,
        )

    async def _readable(self, binding: _Binding) -> None:
        runtime = self._application.runtime
        try:
            if binding.source is not None:
                await runtime.records.for_scope(self._scope).get_source(
                    binding.source.source_type, binding.source.source_id
                )
            elif binding.memory is not None:
                await runtime.memory.for_scope(self._scope).get(GetMemoryEntryRequest(citation=binding.memory))
            elif binding.topic is not None:
                await runtime.records.for_scope(self._scope).get_artifact_revision(
                    binding.topic.family, binding.topic.artifact_id, binding.topic.revision
                )
        except Exception as error:
            raise SourceLifecycleError("registration reference must exist and be readable in this scope") from error  # noqa: TRY003

    async def _rows(self, connection):
        table = SOURCE_LIFECYCLE_NODES_TABLE
        return {
            str(row["node_id"]): dict(row)
            for row in (await connection.execute(select(table).where(table.c.scope_id == self._scope))).mappings()
        }

    @staticmethod
    def _availability(rows) -> dict[str, bool]:
        available: dict[str, bool] = {}
        visiting: set[str] = set()

        def visit(node_id: str) -> bool:
            if node_id in available:
                return available[node_id]
            if node_id in visiting or node_id not in rows:
                raise SourceLifecycleError("lifecycle dependencies must form a registered DAG")  # noqa: TRY003
            visiting.add(node_id)
            row = rows[node_id]
            binding = _Binding.model_validate_json(row["binding"])
            if binding.kind == "source":
                value = bool(row["available"])
            else:
                support = [visit(parent) for parent in binding.parents]
                value = all(support) if binding.policy == "all" else any(support)
            visiting.remove(node_id)
            available[node_id] = value
            return value

        for node_id in rows:
            visit(node_id)
        return available

    async def _register(self, node_id: str, binding: _Binding) -> None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise SourceLifecycleError("node_id must be a nonblank string")  # noqa: TRY003
        async with self._application.runtime._operation(), self._lock:
            await self._readable(binding)
            table = SOURCE_LIFECYCLE_NODES_TABLE
            async with self._application.database.transaction() as connection:
                rows = await self._rows(connection)
                if node_id in rows:
                    if _Binding.model_validate_json(rows[node_id]["binding"]) != binding:
                        raise SourceLifecycleError("node_id is already bound to a different registration")  # noqa: TRY003
                else:
                    if node_id in binding.parents or any(parent not in rows for parent in binding.parents):
                        raise SourceLifecycleError("parents must be distinct previously registered nodes")  # noqa: TRY003
                    state = self._availability(rows)
                    support = [state[parent] for parent in binding.parents]
                    available = binding.kind == "source" or (all(support) if binding.policy == "all" else any(support))
                    await connection.execute(
                        insert(table).values(
                            scope_id=self._scope,
                            node_id=node_id,
                            kind=binding.kind,
                            artifact_id=None if binding.topic is None else binding.topic.artifact_id,
                            binding=binding.model_dump_json().encode(),
                            available=available,
                            sequence=0,
                        )
                    )
                rows = await self._rows(connection)
            await self._propagate(rows, reason="source lifecycle registration")

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("apply requires a validated SourceLifecycleEvent")  # noqa: TRY003
        async with self._application.runtime._operation(), self._lock:
            table = SOURCE_LIFECYCLE_NODES_TABLE
            async with self._application.database.transaction() as connection:
                rows = await self._rows(connection)
                row = rows.get(event.node_id)
                if row is None:
                    raise SourceLifecycleError("unknown source node")  # noqa: TRY003
                binding = _Binding.model_validate_json(row["binding"])
                if binding.kind != "source" or binding.revision != event.revision:
                    raise SourceLifecycleError("event must match the registered Source lifecycle revision")  # noqa: TRY003
                if event.sequence < row["sequence"]:
                    raise SourceLifecycleError("event sequence is obsolete")  # noqa: TRY003
                if event.sequence == row["sequence"]:
                    if row["event"] is None or SourceLifecycleEvent.model_validate_json(row["event"]) != event:
                        raise SourceLifecycleError("same event sequence has different content")  # noqa: TRY003
                else:
                    await connection.execute(
                        update(table)
                        .where(table.c.scope_id == self._scope, table.c.node_id == event.node_id)
                        .values(
                            sequence=event.sequence, event=event.model_dump_json().encode(), available=event.available
                        )
                    )
                rows = await self._rows(connection)
            # Persist the accepted event before native state writes. An interrupted
            # propagation replays this exact event and reconciles real native state;
            # successful replays use Memory's state-transition no-op semantics.
            return await self._propagate(rows, reason=event.reason)

    async def _propagate(self, rows, *, reason: str) -> list[str]:
        states = self._availability(rows)
        runtime = self._application.runtime
        memories = {
            node_id: _Binding.model_validate_json(row["binding"])
            for node_id, row in rows.items()
            if row["kind"] == "memory"
        }
        if memories:
            async with runtime._context(self._scope) as context, runtime._locked(self._scope):
                service = context.artifacts.memory
                current = await service.head(context.artifacts.memory_artifact_id)
                entries = {entry.entry_id: entry for entry in await service.entries(current)}
                for available in (False, True):
                    selected = []
                    for node_id, binding in memories.items():
                        citation = binding.memory
                        if citation is None or states[node_id] != available:
                            continue
                        entry = entries.get(citation.entry_id)
                        if entry is None or entry.entry_version_id != citation.entry_version_id:
                            raise SourceLifecycleError("registered Memory body version has changed")  # noqa: TRY003
                        selected.append(entry)
                    if selected:
                        transition = service.reactivate if available else service.forget
                        current = await transition(current, entries=tuple(selected), reason=reason)
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._application.database.transaction() as connection:
            for node_id, available in states.items():
                await connection.execute(
                    update(table)
                    .where(table.c.scope_id == self._scope, table.c.node_id == node_id)
                    .values(available=available)
                )
        return sorted(node_id for node_id, available in states.items() if available)

    async def resolve(self, node_id: str, *, purpose: str):
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("unknown lifecycle reference purpose")  # noqa: TRY003
        async with self._application.runtime._operation(), self._lock:
            async with self._application.database.transaction() as connection:
                rows = await self._rows(connection)
            if node_id not in rows:
                raise SourceLifecycleError("unknown lifecycle node")  # noqa: TRY003
            if purpose == "generation" and not self._availability(rows)[node_id]:
                raise SourceLifecycleError("node is unavailable for generation")  # noqa: TRY003
            return _Binding.model_validate_json(rows[node_id]["binding"]).reference()
