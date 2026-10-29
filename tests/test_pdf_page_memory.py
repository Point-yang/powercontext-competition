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

"""Complete captured pages remain usable beyond the ordinary Memory body limit."""

from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.builtin.artifacts.memory import (
    MemoryEntryInput,
    MemoryWriteAssessment,
    MemoryWriteRejectionCode,
    MemoryWriteVerdict,
)
from powercontext.builtin.artifacts.memory.canonical import (
    entry_content_bytes,
    entry_content_hash,
    normalize_entry_text,
    normalize_text,
)
from powercontext.builtin.artifacts.memory.errors import InvalidMemoryCandidateError
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.persistence.tables import MEMORY_ENTRY_HEADS_TABLE
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    ReviseMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    parse_pdf_document,
    search_pdf_pages,
)


def _pdf_bytes(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 8 Tf 20 700 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class _PagePipeline:
    def __init__(self, *, forged: bool = False) -> None:
        self.forged = forged

    async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            evidence = TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
            text = "Invented account balance. " * 400 if self.forged else evidence.content
            entries.append(MemoryEntryInput(kind="source-evidence", text=text, sources=(source,)))
        return tuple(entries)


async def _observation(document_id: str, page_text: str):
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    raw = _pdf_bytes(page_text)
    page = parse_pdf_document(PDFDocumentInput(document_id=document_id, content=raw))[0]
    return project_source_for_transport(registry, await registry.resolve(page))


@pytest.mark.parametrize(
    ("document_id", "page_text"),
    [
        ("REPORT_2025_10K", "Detailed account disclosure. " * 400 + "Operating income 240."),
        ("LONG_" + "issuer" * 1500, "Operating income 240."),
    ],
    ids=("long-page-body", "long-document-prefix"),
)
def test_complete_long_page_roundtrips_searches_revises_and_rebuilds(
    tmp_path: Path, document_id: str, page_text: str
) -> None:
    async def scenario() -> None:
        observation = await _observation(document_id, page_text)
        expected = TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)).content
        assert len(expected.encode("utf-8")) > 8192
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'long-page.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Long pages", summary="Full captured evidence", idempotency_key="long-page")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
            memory = runtime.memory.for_scope(scope.scope_id)
            assert (await memory.flush()).processed
            assert not (await memory.flush()).processed
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            citations = await search_pdf_pages(memory, "Operating income", limit=1)
            assert len(citations) == 1
            citation = citations[0]
            entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
            assert entry.text == expected
            assert entry.sources[0].source_id == observation.name
            assert (await memory.list()).entries[0].entry == entry
            no_op = await memory.revise(ReviseMemoryEntryRequest(citation=citation, kind=entry.kind, text=expected))
            assert no_op.memory_ref == citation.memory_ref
            assert no_op.entry is not None and no_op.entry.entry == entry
            with pytest.raises(InvalidMemoryCandidateError, match="complete captured page evidence"):
                await memory.revise(
                    ReviseMemoryEntryRequest(citation=citation, kind=entry.kind, text=expected + " invented")
                )
        async with open_builtin_contexts(config) as contexts:
            context = await contexts.get(scope.scope_id)
            service = context.artifacts.memory
            head = await service.head(context.artifacts.memory_artifact_id)
            assert (await service.entries(head))[0] == entry
            async with contexts.database.transaction() as connection:
                await connection.execute(MEMORY_ENTRY_HEADS_TABLE.delete())
                await connection.exec_driver_sql("DELETE FROM pc_memory_entry_fts")
            await service.rebuild_projections()
            assert (await service.entries(head))[0] == entry
            assert await service.validate_citation(citation) == entry
            results = await service.search("Operating income", memories=(head,), mode="fts", limit=1)
            assert len(results.hits) == 1
            assert results.hits[0].text == expected

    asyncio.run(scenario())


def test_pdf_reference_does_not_permit_an_unrelated_long_generated_body(tmp_path: Path) -> None:
    async def scenario() -> None:
        observation = await _observation("REPORT", "Operating income 240.")
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'forged-page.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline(forged=True)) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Evidence", summary="Validate long body", idempotency_key="forged-page")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
            memory = runtime.memory.for_scope(scope.scope_id)
            with pytest.raises(InvalidMemoryCandidateError, match="complete captured page evidence") as caught:
                await memory.flush()
            assert caught.value.canonical_code == "captured-page-text-mismatch"
            assert (await memory.list()).entries == ()

    asyncio.run(scenario())


def test_ordinary_long_memory_body_remains_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'ordinary.db'}"))
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Ordinary", summary="Original body budget", idempotency_key="ordinary")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            with pytest.raises(InvalidMemoryCandidateError, match="8192 UTF-8 bytes") as caught:
                await memory.remember(RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="x" * 8193),)))
            assert caught.value.canonical_code == "text-too-long"
            assert (await memory.list()).entries == ()

    asyncio.run(scenario())


def test_short_page_canonical_encoding_and_hash_keep_the_existing_format() -> None:
    refs = ({"source_type": "pdf-page", "source_id": "pdf:abc"},)
    expected = (
        '{"artifact_refs":[],"kind":"page","source_refs":[{"source_id":"pdf:abc",'
        '"source_type":"pdf-page"}],"text":"Café"}'
    ).encode()
    assert entry_content_bytes(kind="page", text="  Cafe\u0301 ", source_refs=refs, artifact_refs=()) == expected
    assert (
        entry_content_hash(kind="page", text="  Cafe\u0301 ", source_refs=refs, artifact_refs=())
        == hashlib.sha256(b"powercontext:entry-content:v1\0" + expected).hexdigest()
    )
    with pytest.raises(ValueError, match="8192 UTF-8 bytes"):
        normalize_text("x" * 8193)


def test_complete_page_bound_includes_its_document_identifier() -> None:
    refs = ({"source_type": "pdf-page", "source_id": "pdf:abc"},)
    document_id = "REPORT"
    text = document_id + "\n" + "x" * (1_048_576 - len(document_id) - 1)
    assert normalize_entry_text(text, source_refs=refs) == text
    with pytest.raises(ValueError, match="1048576 UTF-8 bytes"):
        entry_content_hash(kind="page", text=text + "x", source_refs=refs, artifact_refs=())
    with pytest.raises(ValueError, match="8192 UTF-8 bytes"):
        normalize_entry_text("x" * 8193, source_refs=({"source_type": "content", "source_id": "turn"},))


class _AcceptingGate:
    policy_id = "test.long-pdf-gate"

    def __init__(self) -> None:
        self.called = False

    async def assess(self, request, /) -> MemoryWriteAssessment:
        self.called = True
        return MemoryWriteAssessment(verdict=MemoryWriteVerdict.ACCEPT, policy_id=self.policy_id)


def test_long_page_does_not_bypass_the_gate_evidence_budget(tmp_path: Path) -> None:
    async def scenario() -> None:
        observation = await _observation("REPORT", "Operating income 240. " * 500)
        content = TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)).content
        gate = _AcceptingGate()
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'gate.db'}"))
        async with open_builtin_contexts(config, memory_write_gate=gate) as contexts:
            scope = await contexts.scopes.create(
                ScopeDraft(title="Gate", summary="Bounded evidence", idempotency_key="gate")
            )
            await contexts.register_source_definition(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            await contexts.submit_source_observation(
                SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
            )
            context = await contexts.get(scope.scope_id)
            plan = await context.artifacts.memory.plan_remember(
                memory=None,
                sources=(observation,),
                entries=(MemoryEntryInput(kind="page", text=content, sources=(observation,)),),
                mode="append",
            )
            assert plan.commit is None
            assert plan.decision is not None
            assert plan.decision.verdict is MemoryWriteVerdict.HOLD
            assert plan.decision.code is MemoryWriteRejectionCode.EVIDENCE_LIMIT_EXCEEDED
            assert not gate.called

    asyncio.run(scenario())
