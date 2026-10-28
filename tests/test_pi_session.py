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

import pytest

from powercontext.sources import SourceDefinitionRegistry, SourceMaterialization, TextEvidence
from powercontext.sources.pi_session import PI_EVENT_SOURCE_DEFINITION, PiSessionInput, parse_pi_session
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def archive(records: list[dict[str, object]], *, session_id: str = "会话:/%", bom: bool = False) -> bytes:
    header = {"type": "session", "version": 3, "id": session_id}
    content = "\n".join(json.dumps(record, ensure_ascii=False) for record in [header, *records]).encode()
    return (b"\xef\xbb\xbf" if bom else b"") + content


def message(event_id: str, parent_id: str | None, content: str) -> dict[str, object]:
    return {
        "type": "message",
        "id": event_id,
        "parentId": parent_id,
        "message": {"role": "user", "content": content},
    }


def test_visible_event_text_and_original_record_survive_control_nodes_and_summaries() -> None:
    records: list[dict[str, object]] = [
        message("root", None, " 原始\r\n正文 \t"),
        {"type": "model_change", "id": "control", "parentId": "root", "provider": "offline"},
        {
            "type": "message",
            "id": "tool",
            "parentId": "control",
            "timestamp": "2026-10-28T01:02:03Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden reasoning"},
                    {"type": "text", "text": "调用前"},
                    {
                        "type": "toolCall",
                        "name": "read",
                        "id": "调用-1",
                        "arguments": {"z": ["行\n尾", {"é": "字"}], "a": "路径/中文"},
                    },
                    {"type": "image", "data": "hidden image"},
                    {"type": "text", "text": "调用后"},
                ],
                "extra": {"retained": True},
            },
        },
        {
            "type": "message",
            "id": "result",
            "parentId": "tool",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "调用-1",
                "content": [{"type": "text", "text": " 第一行 "}, {"type": "text", "text": "第二行\n"}],
                "isError": False,
            },
        },
        {"type": "compaction", "id": "compact", "parentId": "result", "summary": "压缩摘要"},
        {"type": "branch_summary", "id": "summary", "parentId": "compact", "summary": "分支摘要"},
        {
            "type": "message",
            "id": "bash",
            "parentId": "summary",
            "message": {"role": "bashExecution", "command": "printf '不执行'", "output": "输出\n", "content": []},
        },
        {
            "type": "custom_message",
            "id": "custom",
            "parentId": "bash",
            "content": [{"type": "text", "text": "额外"}, {"type": "text", "text": "证据"}],
        },
        {"type": "thinking_level_change", "id": "leaf", "parentId": "custom"},
        message("other", "root", "另一分支"),
    ]
    events = parse_pi_session(PiSessionInput(content=archive(records, bom=True), leaf_id="leaf"))
    assert isinstance(events, tuple)
    assert [event.event_id for event in events] == ["root", "tool", "result", "compact", "summary", "bash", "custom"]
    assert [event.content for event in events] == [
        "user:  原始\r\n正文 \t",
        'assistant: 调用前\n[tool_call read 调用-1] {"a":"路径/中文","z":["行\\n尾",{"é":"字"}]}\n调用后',
        "toolResult: [tool_result read 调用-1]  第一行 \n第二行\n",
        "compaction: 压缩摘要",
        "branch_summary: 分支摘要",
        "bashExecution: $ printf '不执行'\n输出\n",
        "custom: 额外\n证据",
    ]
    originals = {record["id"]: record for record in records}
    for event in events:
        assert event.record == originals[event.event_id]
        assert event.parent_id == originals[event.event_id]["parentId"]
        assert event.session_id == "会话:/%"
        assert event.event_type == originals[event.event_id]["type"]
    assert events[1].parent_id == "control"
    assert [event.event_id for event in parse_pi_session(PiSessionInput(content=archive(records)))] == ["root", "other"]


@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param(b"\n{broken json}", id="off-branch-invalid-json"),
        pytest.param(b"\n\xff", id="off-branch-invalid-utf8"),
        pytest.param(b'\n{"type":"model_change","id":"other","parentId":"missing"}', id="missing-parent"),
        pytest.param(b'\n{"type":"model_change","id":"other","parentId":"other"}', id="self-parent"),
        pytest.param(b'\n{"type":"model_change","id":"other"}', id="omitted-parent"),
        pytest.param(b'\n{"type":"model_change","id":"root","parentId":null}', id="duplicate-id"),
        pytest.param(b'\n{"type":"model_change","id":"other","parentId":7}', id="wrong-parent-type"),
        pytest.param(b"\n[]", id="non-object-record"),
    ],
)
def test_entire_archive_is_validated_before_returning_even_an_explicit_earlier_leaf(suffix: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=archive([message("root", None, "valid")]) + suffix, leaf_id="root"))


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xef\xbb\xbf",
        b"null\n",
        b' {"type":"session","version":2,"id":"session"}',
        b' {"type":"message","version":3,"id":"session"}',
        b' {"type":"session","version":3,"id":""}',
        b' {"type":"session","version":3,"id":3}',
    ],
)
def test_invalid_session_header_never_produces_partial_results(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=content))


def test_unknown_leaf_is_rejected_and_a_header_only_session_has_no_evidence() -> None:
    assert parse_pi_session(PiSessionInput(content=archive([]))) == ()
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=archive([message("root", None, "body")]), leaf_id="absent"))


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_line_separators_remain_json_string_data_and_session_ids_have_a_separate_namespace(
    separator: str,
) -> None:
    text = f"开始{separator}结束"
    records: list[dict[str, object]] = [
        message("shared-id", None, text),
        {
            "type": "message",
            "id": "block",
            "parentId": "shared-id",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        },
        {"type": "compaction", "id": "summary", "parentId": "block", "summary": text},
        {
            "type": "message",
            "id": "tool",
            "parentId": "summary",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "name": "read", "id": "call", "arguments": {"text": text}}],
            },
        },
    ]
    events = parse_pi_session(PiSessionInput(content=archive(records, session_id="shared-id")))
    assert [event.event_id for event in events] == ["shared-id", "block", "summary", "tool"]
    assert [event.content for event in events] == [
        f"user: {text}",
        f"assistant: {text}",
        f"compaction: {text}",
        f'assistant: [tool_call read call] {{"text":"{text}"}}',
    ]
    assert [event.record for event in events] == records


def test_event_source_identity_is_bounded_unambiguous_and_round_trips_original_evidence() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        identities = set()
        for session_id, event_id in [
            ("a:b", "c"),
            ("a", "b:c"),
            ("中文" * 200, "事件" * 200),
            ("%2F", "/"),
            ("/", "%2F"),
            ("é", "event"),
            ("e\u0301", "event"),
        ]:
            event = parse_pi_session(
                PiSessionInput(content=archive([message(event_id, None, " 原样\n正文 ")], session_id=session_id))
            )[0]
            source = await registry.resolve(event)
            assert source.materialization is SourceMaterialization.CAPTURED
            assert len(source.name) <= 256
            assert source.name not in identities
            identities.add(source.name)
            assert (await registry.resolve(event)).name == source.name
            restored = type(source).model_validate_json(source.model_dump_json())
            assert await registry.read(restored) == event
            evidence = TextEvidence.model_validate(registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY))
            assert evidence.source_type == "pi-event"
            assert evidence.source_id == source.name
            assert evidence.content == event.content
            for field in ("session_id", "event_id", "parent_id", "event_type", "role"):
                assert evidence.metadata[field] == getattr(event, field)

    asyncio.run(scenario())
