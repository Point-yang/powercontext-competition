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

"""Capture the original visible events on a Pi v3 session branch."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, JsonValue, StrictBytes, StrictStr, model_validator

from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Pi {field} must be a non-empty string")  # noqa: TRY003
    return value


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _record_fingerprint(record: dict[str, JsonValue]) -> str:
    return hashlib.sha256(_compact_json(record).encode("utf-8")).hexdigest()


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return a bounded identity without ambiguities between the two Pi IDs."""

    _nonempty_string(session_id, "session id")
    _nonempty_string(event_id, "event id")
    encoded = _compact_json([session_id, event_id]).encode("utf-8")
    return f"pi:{hashlib.sha256(encoded).hexdigest()}"


def _visible_blocks(content: JsonValue, *, tool_calls: bool = False) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        raise ValueError("Pi visible content must be a string or a list of blocks")  # noqa: TRY003, TRY004
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Pi content blocks must be objects")  # noqa: TRY003, TRY004
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("Pi text blocks must contain string text")  # noqa: TRY003
            parts.append(text)
        elif block.get("type") == "toolCall" and tool_calls:
            name = _nonempty_string(block.get("name"), "tool name")
            call_id = _nonempty_string(block.get("id"), "tool call id")
            arguments = _compact_json(block.get("arguments", {}))
            parts.append(f"[tool_call {name} {call_id}] {arguments}")
    return "\n".join(parts)


def _visible_record(record: dict[str, JsonValue]) -> tuple[str, str] | None:
    event_type = _nonempty_string(record.get("type"), "event type")
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("Pi message events must contain a message object")  # noqa: TRY003
        role = _nonempty_string(message.get("role"), "message role")
        if role == "bashExecution":
            command, output = message.get("command", ""), message.get("output", "")
            if not isinstance(command, str) or not isinstance(output, str):
                raise ValueError("Pi bash commands and output must be strings")  # noqa: TRY003
            body = f"$ {command}\n{output}" if command or output else ""
        else:
            body = _visible_blocks(message.get("content"), tool_calls=True)
            if role == "toolResult" and body:
                name = _nonempty_string(message.get("toolName"), "tool result name")
                call_id = _nonempty_string(message.get("toolCallId"), "tool result call id")
                body = f"[tool_result {name} {call_id}] {body}"
    elif event_type in {"compaction", "branch_summary"}:
        role, body = event_type, record.get("summary", "")
        if not isinstance(body, str):
            raise ValueError("Pi summaries must be strings")  # noqa: TRY003
    elif event_type == "custom_message":
        role, body = "custom", _visible_blocks(record.get("content"))
    else:
        return None
    return (role, body) if body else None


def _node_identity(record: dict[str, JsonValue]) -> tuple[str, str | None]:
    event_id = _nonempty_string(record.get("id"), "event id")
    if "parentId" not in record:
        raise ValueError("Pi event records must contain parentId")  # noqa: TRY003
    parent = record["parentId"]
    if parent is not None:
        parent = _nonempty_string(parent, "parent id")
    return event_id, parent


class PiSessionInput(BaseModel):
    """A complete UTF-8 Pi v3 JSONL archive and an optional branch leaf."""

    content: StrictBytes
    leaf_id: StrictStr | None = None


class PiEventInput(BaseModel):
    """One complete original record and its visible textual evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: StrictStr
    event_id: StrictStr
    parent_id: StrictStr | None
    event_type: StrictStr
    role: StrictStr
    content: StrictStr
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_event(self) -> PiEventInput:
        _nonempty_string(self.session_id, "session id")
        event_id, parent_id = _node_identity(self.record)
        visible = _visible_record(self.record)
        if (
            self.event_id != event_id
            or self.parent_id != parent_id
            or self.event_type != self.record.get("type")
            or visible is None
            or self.role != visible[0]
            or self.content != f"{visible[0]}: {visible[1]}"
        ):
            raise ValueError("Pi event fields must describe the complete original visible record")  # noqa: TRY003
        return self


class PiEventSource(Source):
    """Durable, untruncated event evidence behind a stable Pi reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    session_id: StrictStr
    event_id: StrictStr
    parent_id: StrictStr | None
    event_type: StrictStr
    role: StrictStr
    content: StrictStr
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_source(self) -> PiEventSource:
        _event_from_source(self)
        return self


def _event_from_source(source: PiEventSource) -> PiEventInput:
    if source.name != pi_event_source_id(source.session_id, source.event_id):
        raise ValueError("Pi Source name must equal its stable event identity")  # noqa: TRY003
    if source.materialization is not SourceMaterialization.CAPTURED:
        raise ValueError("Pi event Sources must retain captured records")  # noqa: TRY003
    return PiEventInput(
        session_id=source.session_id,
        event_id=source.event_id,
        parent_id=source.parent_id,
        event_type=source.event_type,
        role=source.role,
        content=source.content,
        record=deepcopy(source.record),
    )


def _json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    record: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("Pi JSON objects must not contain duplicate keys")  # noqa: TRY003
        record[key] = value
    return record


def _json_constant(value: str) -> JsonValue:
    raise ValueError(f"Pi JSON must not contain non-finite values: {value}")  # noqa: TRY003


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Pi JSON numbers must be finite")  # noqa: TRY003
    return number


def _decode_records(content: bytes) -> list[dict[str, JsonValue]]:
    try:
        # JSONL separates records with LF. Unicode line separators can occur in
        # valid JSON strings and must remain part of the captured evidence.
        lines = content.decode("utf-8-sig", errors="strict").split("\n")
        records = [
            json.loads(line, object_pairs_hook=_json_object, parse_constant=_json_constant, parse_float=_json_float)
            for line in lines
            if line.strip()
        ]
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Pi session content must be valid UTF-8 JSONL") from error  # noqa: TRY003
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Pi session records must be objects")  # noqa: TRY003
    return records


def _parse_archive(value: PiSessionInput) -> tuple[str, dict[str, dict[str, JsonValue]], tuple[PiEventInput, ...]]:
    records = _decode_records(value.content)
    if not records:
        raise ValueError("Pi session must start with a session header")  # noqa: TRY003
    header = records[0]
    if header.get("type") != "session" or type(header.get("version")) is not int or header.get("version") != 3:
        raise ValueError("Pi session header must declare type=session and version=3")  # noqa: TRY003
    session_id = _nonempty_string(header.get("id"), "session id")
    nodes: dict[str, dict[str, JsonValue]] = {}
    visible_events: dict[str, PiEventInput] = {}
    leaf_id = None
    for record in records[1:]:
        event_id, parent_id = _node_identity(record)
        if event_id in nodes:
            raise ValueError("Pi event IDs must be unique")  # noqa: TRY003
        if parent_id is not None and parent_id not in nodes:
            raise ValueError("Pi parents must point to an earlier event")  # noqa: TRY003
        nodes[event_id] = record
        visible = _visible_record(record)
        if visible is not None:
            role, body = visible
            visible_events[event_id] = PiEventInput(
                session_id=session_id,
                event_id=event_id,
                parent_id=parent_id,
                event_type=cast("str", record["type"]),
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        leaf_id = event_id
    if value.leaf_id is not None:
        leaf_id = value.leaf_id
        if leaf_id not in nodes:
            raise ValueError("Pi selected leaf does not exist")  # noqa: TRY003
    return session_id, nodes, _ancestor_events(nodes, visible_events, leaf_id)


def _ancestor_events(
    nodes: dict[str, dict[str, JsonValue]], visible_events: dict[str, PiEventInput], leaf_id: str | None
) -> tuple[PiEventInput, ...]:
    chain: list[PiEventInput] = []
    while leaf_id is not None:
        if leaf_id in visible_events:
            chain.append(visible_events[leaf_id])
        leaf_id = cast("str | None", nodes[leaf_id]["parentId"])
    chain.reverse()
    return tuple(chain)


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Validate the entire archive, then return the chosen branch's evidence."""

    return _parse_archive(value)[2]


class _PiEventSourceAdapter:
    input_class = PiEventInput
    name = "pi-event"
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=pi_event_source_id(value.session_id, value.event_id),
            **value.model_dump(mode="python"),
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return _event_from_source(source)


class _PiEventTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        _event_from_source(source)
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    _PiEventSourceAdapter(),
    projections=(_PiEventTextEvidenceProjection(),),
)


def _load_checkpoint(value: JsonValue | None) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "sessions"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["sessions"], dict)
    ):
        raise ValueError("Invalid Pi history checkpoint")  # noqa: TRY003
    sessions: dict[str, dict[str, str]] = {}
    for session_id, fingerprints in value["sessions"].items():
        _nonempty_string(session_id, "checkpoint session id")
        if not isinstance(fingerprints, dict):
            raise ValueError("Invalid Pi checkpoint event map")  # noqa: TRY003, TRY004
        sessions[session_id] = {}
        for event_id, fingerprint in fingerprints.items():
            _nonempty_string(event_id, "checkpoint event id")
            if (
                not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in fingerprint)
            ):
                raise ValueError("Invalid Pi checkpoint record fingerprint")  # noqa: TRY003
            sessions[session_id][event_id] = fingerprint
    return sessions


class PiSessionConnector:
    """Incrementally import a branch using only the lifecycle's durable progress."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = await asyncio.to_thread(self.path.read_bytes)
        session_id, nodes, events = _parse_archive(PiSessionInput(content=content, leaf_id=self.leaf_id))
        sessions = _load_checkpoint(session.checkpoint)
        imported = sessions.setdefault(session_id, {})
        # Verify the entire already-imported history, including the other branch,
        # before making any durable submissions from this run.
        for event_id, fingerprint in imported.items():
            record = nodes.get(event_id)
            if record is None or _record_fingerprint(record) != fingerprint:
                raise ValueError("A previously imported Pi event was removed or changed")  # noqa: TRY003
        for event in events:
            if event.event_id in imported:
                continue
            fingerprint = _record_fingerprint(event.record)
            outcome = await session.submit(pi_event_source_id(session_id, event.event_id), "pi-event", event)
            if outcome.status is not ConnectorSubmissionStatus.ACCEPTED:
                return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE, checkpoint=session.checkpoint)
            imported[event.event_id] = fingerprint
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"version": 1, "sessions": sessions},
        )


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
