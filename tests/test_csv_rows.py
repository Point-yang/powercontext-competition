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
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def test_distinct_space_sensitive_headers_and_keys_retain_original_values() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="原始表",
            primary_key=" id ",
            content=(
                "id, id ,number,empty,bool,date,note\n"
                'ignored, 01 ,001,,true,2026-01-02," 原样 中文 "\n'
                "ignored,01 ,0,,false,2026-01-02,NULL\n"
                "ignored,01,1.50,,False,2026-01-02,null\n"
            ).encode(),
        )
    )
    assert tuple(row.primary_key_value for row in rows) == (" 01 ", "01 ", "01")
    assert rows[0].columns == ("id", " id ", "number", "empty", "bool", "date", "note")
    assert rows[0].values == ("ignored", " 01 ", "001", "", "true", "2026-01-02", " 原样 中文 ")
    assert len({csv_row_source_id(row.document_id, row.primary_key_value) for row in rows}) == 3


@pytest.mark.parametrize("primary_key", ["id", " id", "id "])
def test_primary_key_must_match_the_untrimmed_header(primary_key: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(
            CSVDocumentInput(document_id="table", primary_key=primary_key, content=b" id ,value\n1,one\n")
        )


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
def test_record_endings_and_quoted_blank_lines_are_preserved(newline: str) -> None:
    quoted_value = f"first{newline}{newline}last"
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="table",
            primary_key="id",
            content=f'id,note{newline}{newline}1,"{quoted_value}"{newline}{newline}'.encode(),
        )
    )
    assert len(rows) == 1
    assert rows[0].values == ("1", quoted_value)


def test_bom_is_accepted_only_as_file_prefix_and_inner_bom_remains_data() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="table",
            primary_key="id",
            content="\ufeffid,\ufeffnote\n\ufeff01,\ufeffvalue\n".encode(),
        )
    )
    assert rows[0].columns == ("id", "\ufeffnote")
    assert rows[0].primary_key_value == "\ufeff01"
    assert rows[0].values == ("\ufeff01", "\ufeffvalue")


@pytest.mark.parametrize(
    ("record", "expected_value"),
    [
        (b'1,bare"quote\n', 'bare"quote'),
        (b'1, "quoted"\n', ' "quoted"'),
        (b'1,"""quoted"""', '"quoted"'),
        (b'1,""\n', ""),
    ],
)
def test_csv_reader_compatible_quote_and_empty_value_semantics(record: bytes, expected_value: str) -> None:
    rows = parse_csv_document(CSVDocumentInput(document_id="table", primary_key="id", content=b"id,note\n" + record))
    assert rows[0].values == ("1", expected_value)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"id,note\n1,valid\n2,\xff\n", id="late-invalid-utf8"),
        pytest.param(b'id,note\n1,valid\n2,"unclosed', id="late-unclosed-quote"),
        pytest.param(b'id,note\n1,valid\n2,"closed"suffix\n', id="late-malformed-closing-quote"),
        pytest.param(b"id,note\n1,valid\n2", id="late-short-record"),
        pytest.param(b"id,note\n1,valid\n2,too,many\n", id="late-wide-record"),
        pytest.param(b"id,note\n1,valid\n1,duplicate\n", id="late-duplicate-key"),
        pytest.param(b"id,note\n1,valid\n,empty-key\n", id="late-empty-key"),
        pytest.param("id,note\n1,valid\n\u3000,blank-key\n".encode(), id="late-unicode-blank-key"),
        pytest.param(b"id,note\n1,valid\n" + b"x" * 251 + b",long-key\n", id="late-oversized-identity"),
        pytest.param(b'id\n1\n""\n', id="quoted-empty-record-is-not-skipped"),
        pytest.param(b"id\n1\n \n", id="space-only-record-is-not-skipped"),
        pytest.param(b"\nid,note\n1,valid\n", id="empty-first-record-is-not-skipped"),
        pytest.param(b"id,note, note\n1,one\n", id="header-width-is-authoritative"),
        pytest.param(b"\xef\xbb\xbf", id="bom-only-is-not-header"),
    ],
)
def test_invalid_documents_fail_eagerly_instead_of_returning_partial_rows(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="d", primary_key="id", content=content))


def test_identity_percent_encoding_is_unicode_safe_and_collision_free() -> None:
    assert csv_row_source_id("中文:/%", " 键:/% ") == ("csv:%E4%B8%AD%E6%96%87%3A%2F%25:%20%E9%94%AE%3A%2F%25%20")
    assert csv_row_source_id("d:a", "b") != csv_row_source_id("d", "a:b")
    assert csv_row_source_id("d", "%2F") != csv_row_source_id("d", "/")
    assert csv_row_source_id("d", "é") != csv_row_source_id("d", "e\u0301")


@pytest.mark.parametrize(
    ("document_id", "key_at_limit"),
    [
        ("d", "a" * 250),
        ("表", "a" * 242),
        ("d", "/" * 83 + "a"),
    ],
)
def test_identity_limit_applies_to_percent_encoded_length(document_id: str, key_at_limit: str) -> None:
    assert len(csv_row_source_id(document_id, key_at_limit)) == 256
    row = parse_csv_document(
        CSVDocumentInput(document_id=document_id, primary_key="id", content=f"id\n{key_at_limit}\n".encode())
    )[0]
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))
    assert isinstance(source, CSVRowSource)
    assert len(source.source_id) == 256
    with pytest.raises(ValueError):
        csv_row_source_id(document_id, key_at_limit + "a")


def test_captured_source_json_round_trip_reads_and_projects_exact_original_row() -> None:
    row = CSVRowInput(
        document_id="中文:records",
        primary_key=" id ",
        primary_key_value=" 001 ",
        columns=("quote", " id ", "empty", "note", "not-number"),
        values=('a"b', " 001 ", "", "\r\n中文\n", "0001"),
    )
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))
    restored = CSVRowSource.model_validate_json(source.model_dump_json())
    assert restored.materialization is SourceMaterialization.CAPTURED
    assert restored.name == restored.source_id == csv_row_source_id(row.document_id, row.primary_key_value)
    assert asyncio.run(registry.read(restored)) == row

    evidence = TextEvidence.model_validate(registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY))
    expected = dict(zip(row.columns, row.values, strict=True))
    assert evidence.content == json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    assert evidence.metadata == {
        "document_id": row.document_id,
        "primary_key": row.primary_key,
        "primary_key_value": row.primary_key_value,
        "columns": list(row.columns),
        "row": expected,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"values": ("1",)},
        {"columns": ("id", "id")},
        {"columns": ("id", "\t ")},
        {"primary_key": "missing"},
        {"primary_key_value": "2"},
    ],
)
def test_direct_row_input_cannot_resolve_inconsistent_record(changes: dict[str, object]) -> None:
    values = {
        "document_id": "table",
        "primary_key": "id",
        "primary_key_value": "1",
        "columns": ("id", "note"),
        "values": ("1", "original"),
    }
    values.update(changes)
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    with pytest.raises(ValueError):
        asyncio.run(registry.resolve(CSVRowInput(**values)))
