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

"""PDF text keeps its canonical form through captured evidence and Memory."""

from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _unicode_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    mapping = DecodedStreamObject()
    mapping.set_data(b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /UnicodeEvidence def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
2 beginbfchar
<80> <00650301>
<81> <FB01>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end
""")
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        NameObject("/ToUnicode"): writer._add_object(mapping),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    text = b"Operating revenue \x80 and \x81"
    stream.set_data(f"BT /F1 12 Tf 20 700 Td <{text.hex()}> Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_blank_page(width=600, height=800)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class _PagePipeline:
    async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(
                kind="source-evidence",
                text=TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY)).content,
                sources=(source,),
            )
            for source in request.sources
        )


def test_decomposed_pdf_text_matches_its_exact_memory_citation_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        raw = _unicode_pdf()
        assert PdfReader(io.BytesIO(raw)).pages[0].extract_text() == "Operating revenue e\u0301 and \ufb01"
        pages = parse_pdf_document(PDFDocumentInput(document_id="REPORT", content=raw))
        assert [page.page_number for page in pages] == [1, 2]
        assert all(page.document_id == "REPORT" for page in pages)
        assert all(page.document_sha256 == hashlib.sha256(raw).hexdigest() for page in pages)
        assert pages[1].text == ""
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        sources = [await registry.resolve(page) for page in pages]
        assert [await registry.read(source) for source in sources] == list(pages)
        observations = [project_source_for_transport(registry, source) for source in sources]
        projection = TextEvidence.model_validate(observations[0].projection(TEXT_EVIDENCE_PROJECTION_KEY))
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'unicode.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Unicode PDF", summary="Exact page evidence", idempotency_key="unicode-pdf")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            for _ in range(len(observations) + 1):
                if not (await runtime.memory.for_scope(scope.scope_id).flush()).processed:
                    break
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            citations = await search_pdf_pages(memory, "Operating revenue", limit=1)
            assert len(citations) == 1
            entry = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
            assert entry.text == projection.content.strip()
            assert entry.text == "REPORT\nOperating revenue é and \ufb01"
            assert entry.sources[0].source_id == sources[0].name
            assert pages[0].text == "Operating revenue é and \ufb01"

    asyncio.run(scenario())


def test_direct_page_inputs_and_source_reads_preserve_their_original_unicode() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        page = PDFPageInput(document_id="Re\u0301port", document_sha256="a" * 64, page_number=1, text="e\u0301 \ufb01")
        source = await registry.resolve(page)
        restored = type(source).model_validate_json(source.model_dump_json())
        readback = await registry.read(restored)
        assert isinstance(readback, PDFPageInput)
        assert readback == page
        assert readback.document_id == "Re\u0301port"
        assert readback.text == "e\u0301 \ufb01"

    asyncio.run(scenario())
