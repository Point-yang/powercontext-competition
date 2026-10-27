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

"""Capture complete CSV records with stable, document-scoped identities."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, StrictBytes, StrictStr, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _validate_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty string without surrounding whitespace")  # noqa: TRY003


def _validate_primary_key(primary_key: str) -> None:
    if not isinstance(primary_key, str) or not primary_key:
        raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003


def _primary_key_index(columns: tuple[str, ...], primary_key: str) -> int:
    _validate_primary_key(primary_key)
    if not columns or any(not isinstance(column, str) or not column.strip() for column in columns):
        raise ValueError("CSV columns must be non-empty, non-blank strings")  # noqa: TRY003
    if len(set(columns)) != len(columns):
        raise ValueError("CSV columns must be unique")  # noqa: TRY003
    try:
        return columns.index(primary_key)
    except ValueError as error:
        raise ValueError("primary_key must exactly match a CSV column") from error  # noqa: TRY003


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Encode a document and its exact primary-key value without collisions."""

    _validate_document_id(document_id)
    if not isinstance(primary_key_value, str) or not primary_key_value.strip():
        raise ValueError("primary_key_value must be a non-blank string")  # noqa: TRY003
    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("CSV row identity must not exceed 256 characters")  # noqa: TRY003
    return identity


def _validate_row(
    document_id: str,
    primary_key: str,
    primary_key_value: str,
    columns: tuple[str, ...],
    values: tuple[str, ...],
) -> str:
    identity = csv_row_source_id(document_id, primary_key_value)
    primary_key_index = _primary_key_index(columns, primary_key)
    if len(values) != len(columns) or any(not isinstance(value, str) for value in values):
        raise ValueError("CSV row must contain one string value per column")  # noqa: TRY003
    if values[primary_key_index] != primary_key_value:
        raise ValueError("primary_key_value must equal the corresponding CSV field")  # noqa: TRY003
    return identity


class CSVDocumentInput(BaseModel):
    """One complete offline CSV document and its exact primary-key column."""

    document_id: StrictStr
    primary_key: StrictStr
    content: StrictBytes


class CSVRowInput(BaseModel):
    """An ordered, string-preserving record captured from a CSV document."""

    document_id: StrictStr
    primary_key: StrictStr
    primary_key_value: StrictStr
    columns: tuple[StrictStr, ...]
    values: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def validate_row(self) -> CSVRowInput:
        _validate_row(self.document_id, self.primary_key, self.primary_key_value, self.columns, self.values)
        return self


class CSVRowSource(Source):
    """The complete captured record behind one stable row reference."""

    source_id: StrictStr
    document_id: StrictStr
    primary_key: StrictStr
    primary_key_value: StrictStr
    columns: tuple[StrictStr, ...]
    values: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def validate_row(self) -> CSVRowSource:
        _validate_source(self)
        return self


def _validate_source(source: CSVRowSource) -> None:
    identity = _validate_row(
        source.document_id, source.primary_key, source.primary_key_value, source.columns, source.values
    )
    if source.source_id != identity or source.name != identity:
        raise ValueError("CSV Source name and source_id must equal its row identity")  # noqa: TRY003
    if source.materialization != SourceMaterialization.CAPTURED:
        raise ValueError("CSV row Sources must contain captured records")  # noqa: TRY003


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Validate the entire CSV before returning any independently captured rows."""

    _validate_document_id(value.document_id)
    _validate_primary_key(value.primary_key)
    try:
        # newline="" preserves line endings inside quoted fields, including CRLF.
        reader = csv.reader(io.StringIO(value.content.decode("utf-8-sig", errors="strict"), newline=""), strict=True)
        header = next(reader, None)
        columns = tuple(header) if header is not None else ()
        primary_key_index = _primary_key_index(columns, value.primary_key)
        rows = []
        primary_keys: set[str] = set()
        for record in reader:
            # Only a truly empty physical record produces []; quoted empties and
            # whitespace records must still pass width and primary-key checks.
            if not record:
                continue
            if len(record) != len(columns):
                raise ValueError("CSV data record width must equal the header width")  # noqa: TRY003
            primary_key_value = record[primary_key_index]
            if primary_key_value in primary_keys:
                raise ValueError("CSV primary-key values must be unique within the document")  # noqa: TRY003
            row = CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
            primary_keys.add(primary_key_value)
            rows.append(row)
        return tuple(rows)
    except (UnicodeError, csv.Error) as error:
        raise ValueError("CSV content must be valid UTF-8 and well-formed CSV") from error  # noqa: TRY003


class _CSVRowSourceAdapter:
    input_class = CSVRowInput
    name = "csv-row"
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        identity = _validate_row(
            value.document_id, value.primary_key, value.primary_key_value, value.columns, value.values
        )
        return CSVRowSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        _validate_source(source)
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class _CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        _validate_source(source)
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type="csv-row",
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    _CSVRowSourceAdapter(),
    projections=(_CSVRowTextEvidenceProjection(),),
)

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
