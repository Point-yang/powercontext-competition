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
import hashlib
import io
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.builtin.artifacts.memory import MemoryCitation, MemoryEntryInput
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
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def pdf_bytes(texts: tuple[str, ...], password: str | None = None) -> bytes:
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        if text:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
            })
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner")  # noqa: S106 - Generated test PDF only.
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def test_physical_pages_keep_full_text_blank_pages_and_original_digest() -> None:
    raw = pdf_bytes(("Revenue 123.45 negative -7", "", "Last physical page"))
    pages = parse_pdf_document(PDFDocumentInput(document_id=" 年报 ", content=raw))
    assert [page.page_number for page in pages] == [1, 2, 3]
    assert [page.text for page in pages] == ["Revenue 123.45 negative -7", "", "Last physical page"]
    assert all(page.document_sha256 == hashlib.sha256(raw).hexdigest() for page in pages)
    assert all(page.document_id == " 年报 " for page in pages)
    assert parse_pdf_document(PDFDocumentInput(document_id=" 年报 ", content=raw)) == pages


def test_empty_user_password_is_allowed_but_a_required_password_is_not() -> None:
    assert parse_pdf_document(PDFDocumentInput(document_id="open", content=pdf_bytes(("Readable",), "")))[0].text == (
        "Readable"
    )
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="locked", content=pdf_bytes(("Secret",), "private")))


@pytest.mark.parametrize("document_id", ["", " ", "\n\t"])
def test_blank_document_identifiers_are_rejected(document_id: str) -> None:
    with pytest.raises(ValueError):
        PDFDocumentInput(document_id=document_id, content=pdf_bytes(("text",)))


def test_page_identity_is_bounded_revision_specific_and_exactly_readable() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        identities = set()
        for document_id, digest, page_number in [
            ("a:b", "a" * 64, 1),
            ("a", "b" * 64, 1),
            ("a:b", "a" * 64, 2),
            ("a:b", "c" * 64, 1),
            ("文档:/%" * 300, "a" * 64, 1),
        ]:
            page = PDFPageInput(
                document_id=document_id, document_sha256=digest, page_number=page_number, text=" 原始正文\n末尾 "
            )
            source = await registry.resolve(page)
            assert len(source.name) <= 256 and source.name not in identities
            identities.add(source.name)
            assert await registry.read(source) == page
            restored = type(source).model_validate_json(source.model_dump_json())
            assert await registry.read(restored) == page
            evidence = project_source_for_transport(registry, source).projection(TEXT_EVIDENCE_PROJECTION_KEY)
            assert isinstance(evidence, dict)
            assert evidence["content"] == f"{document_id}\n{page.text}"

    asyncio.run(scenario())


class _EvidencePipeline:
    async def extract(self, request, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(
                kind="pdf", text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"], sources=(source,)
            )
            for source in request.sources
        )


def test_search_uses_persisted_scope_and_returns_exact_page_citations_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pdf.db'}"))
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        pages = parse_pdf_document(
            PDFDocumentInput(document_id="REPORT", content=pdf_bytes(("Operating income 240", "Employee benefits 10")))
        )
        observations = [project_source_for_transport(registry, await registry.resolve(page)) for page in pages]
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(ScopeDraft(title="PDF", summary="Pages", idempotency_key="pdf"))
            other_scope = await runtime.scopes.create(
                ScopeDraft(title="Empty", summary="Empty", idempotency_key="empty")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
        async with open_builtin_runtime(config, candidate_pipeline=_EvidencePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            for _ in range(3):
                if not (await memory.flush()).processed:
                    break
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            citations = await search_pdf_pages(memory, "What is the operating income?", limit=1)
            assert len(citations) == 1 and isinstance(citations[0], MemoryCitation)
            entry = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
            assert entry.text == "REPORT\nOperating income 240"
            assert entry.sources[0].source_id == observations[0].name
            empty_scope = runtime.memory.for_scope(other_scope.scope_id)
            assert await search_pdf_pages(empty_scope, "operating income", limit=1) == ()
            with pytest.raises(ValueError):
                await search_pdf_pages(memory, "operating income", limit=True)

    asyncio.run(scenario())


def test_named_company_evidence_is_not_displaced_by_other_companies_short_pages(tmp_path: Path) -> None:
    async def scenario() -> None:
        target = parse_pdf_document(
            PDFDocumentInput(
                document_id="X7_2023_10K",
                content=pdf_bytes(("General accounting disclosures. " * 100 + "Operating income 240.",)),
            )
        )
        unrelated = parse_pdf_document(
            PDFDocumentInput(
                document_id="OTHER_2023_10K",
                content=pdf_bytes(tuple("Operating income operating income 2023." for _ in range(25))),
            )
        )
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        observations = [
            project_source_for_transport(registry, await registry.resolve(page)) for page in (*target, *unrelated)
        ]
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'companies.db'}"))
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Company reports", summary="Separate issuers", idempotency_key="companies")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
        async with open_builtin_runtime(config, candidate_pipeline=_EvidencePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            for _ in range(len(observations) + 1):
                if not (await memory.flush()).processed:
                    break
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            citations = await search_pdf_pages(memory, "What is X7's operating income for 2023?", limit=1)
            assert len(citations) == 1
            entry = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
            assert entry.sources[0].source_id == observations[0].name

    asyncio.run(scenario())


def test_requested_statements_survive_repetitive_notes_and_cover_multiple_evidence_aspects(tmp_path: Path) -> None:
    async def scenario() -> None:
        pages = parse_pdf_document(
            PDFDocumentInput(
                document_id="NORTH_2025_10K",
                content=pdf_bytes((
                    "Consolidated Statements of\nOperations and Comprehensive Income\nRevenue 900. Operating profit 70.",
                    "Condensed Statements of Cash Flows\nDepreciation and amortization 20. Purchases of equipment 30.",
                    "Combined Statements of Financial Position\nAssets 800. Liabilities 300. Equity 500.",
                    "Consolidated Statement of Comprehensive Income\nOther comprehensive income 6.",
                    "Notes to financial instruments\nIncome Statement\n"
                    "Restructuring costs NORTH income statement 2025 EBITDA margin. " * 5,
                    *(
                        "Note: financial definitions\n"
                        "Restructuring costs income statement EBITDA margin cash flow balance sheet 2025. " * 5
                        for _ in range(25)
                    ),
                )),
            )
        )
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        observations = [project_source_for_transport(registry, await registry.resolve(page)) for page in pages]
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'statements.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_EvidencePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Statements", summary="Complete evidence", idempotency_key="statements")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            memory = runtime.memory.for_scope(scope.scope_id)
            for _ in range(len(observations) + 1):
                if not (await memory.flush()).processed:
                    break
            for question, expected, limit in (
                ("Does NORTH show restructuring costs in its 2025 income statement?", {observations[0].name}, 1),
                ("Calculate NORTH's 2025 EBITDA margin using P&L and D&A.", {o.name for o in observations[:2]}, 4),
                ("What are NORTH's liabilities on the 2025 balance sheet?", {observations[2].name}, 1),
            ):
                citations = await search_pdf_pages(memory, question, limit=limit)
                entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
                assert expected <= {source.source_id for entry in entries for source in entry.sources}
                assert len(citations) == limit
                assert len({(citation.entry_id, citation.entry_version_id) for citation in citations}) == limit

    asyncio.run(scenario())


def test_explicit_report_year_selects_issuer_revision_without_treating_every_fiscal_year_as_filing_year(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        inputs = (
            ("NORTH_2025_10K", ("Consolidated Statements of Income\nOperating income 70 in fiscal 2024.",)),
            (
                "NORTH_2024_10K",
                tuple(
                    "Consolidated Statements of Income\nOperating income NORTH 2025 annual report 60."
                    for _ in range(25)
                ),
            ),
            ("SOUTH_2025_10K", ("Warranty accruals changed by 90 in 2024.",)),
            ("SOUTH_2024_10K", ("Other operating income 2024.",)),
        )
        observations = []
        for document_id, texts in inputs:
            for page in parse_pdf_document(PDFDocumentInput(document_id=document_id, content=pdf_bytes(texts))):
                observations.append(project_source_for_transport(registry, await registry.resolve(page)))
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'report-years.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_EvidencePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Annual reports", summary="Distinct filing years", idempotency_key="report-years")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            memory = runtime.memory.for_scope(scope.scope_id)
            for _ in range(len(observations) + 1):
                if not (await memory.flush()).processed:
                    break
            for question in (
                "According to NORTH's 2025 annual report, what is its operating income?",
                "According to NORTH's annual report for 2025, what is its operating income in FY2024?",
                "According to NORTH's 2025 Form 10-K, what is its operating income?",
            ):
                citations = await search_pdf_pages(memory, question, limit=20)
                entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
                assert entries[0].sources[0].source_id == observations[0].name
            citations = await search_pdf_pages(
                memory, "What was SOUTH's change in warranty accruals for FY2024?", limit=1
            )
            entry = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
            assert entry.text.startswith("SOUTH_2025_10K\nWarranty accruals")

    asyncio.run(scenario())


def test_financial_metric_questions_recall_component_statements_without_literal_metric_names(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        pages = parse_pdf_document(
            PDFDocumentInput(
                document_id="SOUTH_2025_10K",
                content=pdf_bytes((
                    "Consolidated Balance Sheets\nInventory 91. Accounts receivable 200. Accounts payable 80. "
                    "Current assets 400. Current liabilities 160.",
                    "Consolidated Statements of Income\nNet revenue 900. Cost of sales 500.",
                    "Consolidated Statements of Cash Flows\nNet cash provided by operating activities 70. "
                    "Capital expenditures 30.",
                )),
            )
        )
        observations = [project_source_for_transport(registry, await registry.resolve(page)) for page in pages]
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'financial-metrics.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_EvidencePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(
                    title="Financial metrics", summary="Underlying statement evidence", idempotency_key="metrics"
                )
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            memory = runtime.memory.for_scope(scope.scope_id)
            for _ in range(len(observations) + 1):
                if not (await memory.flush()).processed:
                    break
            for question, expected in (
                ("What is SOUTH's cash conversion cycle?", {o.name for o in observations[:2]}),
                ("Calculate SOUTH's current ratio.", {observations[0].name}),
                ("What is SOUTH's working capital?", {observations[0].name}),
                ("What is SOUTH's free cash flow?", {observations[2].name}),
            ):
                citations = await search_pdf_pages(memory, question, limit=20)
                entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
                assert expected <= {source.source_id for entry in entries for source in entry.sources}

    asyncio.run(scenario())


async def _search_complete_report_pages(
    tmp_path: Path, texts: tuple[str, ...], questions: tuple[tuple[str, int], ...]
) -> list[tuple[int, ...]]:
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    pages = parse_pdf_document(PDFDocumentInput(document_id="OMEGA_2025_10K", content=pdf_bytes(texts)))
    observations = [project_source_for_transport(registry, await registry.resolve(page)) for page in pages]
    page_by_source = {observation.name: page.page_number for observation, page in zip(observations, pages, strict=True)}
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'statement-coverage.db'}"))
    async with open_builtin_runtime(config, candidate_pipeline=_EvidencePipeline()) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="Statements", summary="Complete page coverage", idempotency_key="statement-coverage")
        )
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for observation in observations:
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
        memory = runtime.memory.for_scope(scope.scope_id)
        for _ in range(len(observations) + 1):
            if not (await memory.flush()).processed:
                break
    results = []
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        for question, limit in questions:
            citations = await search_pdf_pages(memory, question, limit=limit)
            entries = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry for citation in citations]
            numbers = tuple(page_by_source[entry.sources[0].source_id] for entry in entries)
            assert len(numbers) == len(set(numbers)) <= limit
            results.append(numbers)
    return results


def test_cash_flow_continuations_are_not_limited_to_two_pages(tmp_path: Path) -> None:
    texts = (
        "Consolidated Statement of Cash Flows\nNet cash from operating activities 100.",
        "Consolidated Statement of Cash Flows\nFree cash from operating activities 200. "
        "Operating activities capital expenditures reference.",
        "Consolidated Statement of Cash Flows\nPurchases of property plant and equipment 23.",
        *(
            f"Note {index}\n" + "Free cash flow operating activities capital expenditures cash flows 2025. " * 8
            for index in range(25)
        ),
    )
    results = asyncio.run(
        _search_complete_report_pages(
            tmp_path,
            texts,
            (("What is OMEGA free cash flow for FY2025?", 20), ("What is OMEGA free cash flow for FY2025?", 1)),
        )
    )
    assert {1, 2, 3} <= set(results[0])
    assert len(results[0]) == 20
    assert len(results[1]) == 1


def test_quantitative_financial_questions_cover_statements_without_named_metric_templates(tmp_path: Path) -> None:
    texts = (
        "Consolidated Statements of Income\nRevenue 900. Cost of sales 500. Earnings 70.",
        "Consolidated Balance Sheets\nAssets 800. Liabilities 300. Equity 500. Accounts payable 80.",
        "Consolidated Statements of Cash Flows\nPayment to acquire common stock 70. "
        "Purchases of property plant and equipment 23.",
        "Consolidated Statements of Shareholders Equity\nCommon shares acquired for treasury 70.",
        "Geographic segments\nOMEGA geographies: North America Europe Asia. Countries and operations worldwide.",
        "Legal proceedings\nOMEGA reported materially important ongoing legal battles in 2025 and 2024. "
        "Legal risks related to cash payments include litigation.",
        *(
            f"Note {index}\n" + "How much cash OMEGA stock buybacks 2025 capital intensity revenue. " * 8
            for index in range(25)
        ),
    )
    questions = (
        ("How much cash did OMEGA use for stock buybacks in FY2025?", 20),
        ("What was OMEGA capital intensity in FY2025?", 20),
        ("Calculate OMEGA ROE for FY2025.", 20),
        ("What was OMEGA DPO in FY2025?", 20),
        ("What are the geographies that OMEGA primarily operates in as of FY2025?", 1),
        ("Has OMEGA reported any materially important ongoing legal battles from FY2025 and FY2024?", 1),
        ("What are OMEGA's legal risks related to cash payments?", 1),
        ("How much cash did OMEGA use for stock buybacks in FY2025?", 2),
        ("How much cash did OMEGA use for stock buybacks in FY2025?", 1),
    )
    results = asyncio.run(_search_complete_report_pages(tmp_path, texts, questions))
    assert {3, 4} <= set(results[0])
    assert {1, 3} <= set(results[1])
    assert {1, 2} <= set(results[2])
    assert {1, 2} <= set(results[3])
    assert results[4] == (5,)
    assert results[5] == results[6] == (6,)
    assert len(results[7]) == 2
    assert 3 in results[7]
    assert results[8] == (3,)


def test_statement_lookup_words_do_not_displace_literal_quarterly_repurchase_evidence(tmp_path: Path) -> None:
    lookup_groups = (
        "income operations earnings",
        "balance sheet financial condition",
        "financial position equity",
        "equity shareholders stockholders",
        "income cash flows",
    )
    texts = (
        "Consolidated Statements of Income\nRevenue 900. Cost of sales 500. Earnings 70.",
        "Consolidated Balance Sheets\nAssets 800. Liabilities 300. Equity 500.",
        "Consolidated Statements of Cash Flows\nPayment to acquire common stock 70.",
        "Consolidated Statements of Shareholders Equity\nCommon shares acquired for treasury 70.",
        "Quarterly repurchases of common stock\nQuarter ended March: stock buybacks paid in cash 90. "
        "Quarter ended June: stock buybacks paid in cash 80. "
        "Quarter ended September: stock buybacks paid in cash 70.",
        *(
            f"Note {index}\n"
            + (lookup_groups[index % len(lookup_groups)] + " ") * 30
            + "Cash stock buybacks quarter ended 2025."
            for index in range(25)
        ),
    )
    results = asyncio.run(
        _search_complete_report_pages(
            tmp_path,
            texts,
            (("How much cash did OMEGA use for stock buybacks in the quarter ended FY2025?", 20),),
        )
    )
    assert {3, 5} <= set(results[0])
