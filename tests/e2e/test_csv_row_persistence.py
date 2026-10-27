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
from pathlib import Path

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceObservation,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import CSV_ROW_SOURCE_DEFINITION, CSVDocumentInput, parse_csv_document


def test_csv_row_field_order_survives_runtime_restart_and_reordered_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        rows = parse_csv_document(
            CSVDocumentInput(
                document_id="ordered-records",
                primary_key="id",
                content="id,z_note,a_note\n01,原样,001\n02,,false\n".encode(),
            )
        )
        registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
        observations = [project_source_for_transport(registry, await registry.resolve(row)) for row in rows]
        expected = {}
        for observation, row in zip(observations, rows, strict=True):
            evidence = TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY))
            fields = evidence.metadata["row"]
            assert isinstance(fields, dict)
            assert tuple(fields) == row.columns
            expected[observation.name] = evidence

        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'csv-order.db'}"))
        receipts = {}
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Ordered CSV", summary="Preserve original field order", idempotency_key="csv-order")
            )
            scope_id = scope.scope_id
            await runtime.ingestion.register(manifest_for_definition(CSV_ROW_SOURCE_DEFINITION))
            for observation in observations:
                receipts[observation.name] = await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope_id, observation=observation)
                )

        async def assert_persisted_rows() -> None:
            async with open_builtin_contexts(config) as contexts:
                entries = await (await contexts.get(scope_id)).sources.journal.entries()
                assert len(entries) == len(rows)
                assert tuple(entry.source_ref.source_id for entry in entries) == tuple(expected)
                for entry in entries:
                    assert isinstance(entry.source, SourceObservation)
                    evidence = TextEvidence.model_validate(entry.source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
                    assert evidence == expected[entry.source_ref.source_id]
                    fields = evidence.metadata["row"]
                    columns = evidence.metadata["columns"]
                    assert isinstance(fields, dict)
                    assert isinstance(columns, list)
                    assert tuple(fields) == tuple(columns)

        await assert_persisted_rows()

        async with open_builtin_runtime(config) as runtime:
            for observation in reversed(observations):
                reordered = SourceObservation.model_validate(
                    observation.model_dump(mode="json") | {"payload": dict(reversed(observation.payload.items()))}
                )
                replay = await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope_id, observation=reordered)
                )
                assert replay == receipts[observation.name]

        await assert_persisted_rows()

    asyncio.run(scenario())
