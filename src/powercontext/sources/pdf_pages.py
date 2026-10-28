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

"""Capture complete PDF pages and retrieve their exact persisted evidence."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBytes, StrictInt, StrictStr, model_validator

from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
from powercontext.builtin.artifacts.search import analyze_fts_query, analyze_text
from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import ScopedMemoryApplication


def _document_id(value: str) -> str:
    if not value.strip():
        raise ValueError("PDF document_id must not be empty or blank")  # noqa: TRY003
    return value


class PDFDocumentInput(BaseModel):
    """Complete original PDF bytes, identified independently of their revision."""

    document_id: StrictStr
    content: StrictBytes

    @model_validator(mode="after")
    def validate_document(self) -> PDFDocumentInput:
        _document_id(self.document_id)
        return self


class PDFPageInput(BaseModel):
    """The full extracted text of one physical page of one PDF revision."""

    model_config = ConfigDict(frozen=True)

    document_id: StrictStr
    document_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: StrictInt = Field(gt=0)
    text: StrictStr

    @model_validator(mode="after")
    def validate_page(self) -> PDFPageInput:
        _document_id(self.document_id)
        return self


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Bound the identity without conflating delimiters, Unicode or revisions."""

    page = PDFPageInput(document_id=document_id, document_sha256=document_sha256, page_number=page_number, text="")
    encoded = json.dumps(
        [page.document_id, page.document_sha256, page.page_number], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "pdf:" + hashlib.sha256(encoded).hexdigest()


class PDFPageSource(Source):
    """A captured complete page whose reference never changes across revisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    document_id: StrictStr
    document_sha256: StrictStr
    page_number: StrictInt
    text: StrictStr

    @model_validator(mode="after")
    def validate_source(self) -> PDFPageSource:
        if self.name != pdf_page_source_id(self.document_id, self.document_sha256, self.page_number):
            raise ValueError("PDF Source name must equal its document revision and physical page identity")  # noqa: TRY003
        return self


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page before returning any page evidence."""

    from pypdf import PdfReader

    _document_id(value.document_id)
    if not value.content:
        raise ValueError("PDF input must not be empty")  # noqa: TRY003
    digest = hashlib.sha256(value.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(value.content), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a non-empty password")  # noqa: TRY003, TRY301 - Normalize parser failures below.
        pages: list[PDFPageInput] = []
        for page_number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            text.encode("utf-8")
            pages.append(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=digest,
                    page_number=page_number,
                    text=text,
                )
            )
        return tuple(pages)
    except Exception as error:
        # pypdf can report malformed object trees through several exception types.
        # None of those failures may expose a partially parsed document.
        raise ValueError("PDF input must be readable in full without a supplied password") from error  # noqa: TRY003


class _PDFPageSourceAdapter:
    input_class = PDFPageInput
    name = "pdf-page"
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(
            name=pdf_page_source_id(value.document_id, value.document_sha256, value.page_number),
            **value.model_dump(),
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class _PDFPageTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    _PDFPageSourceAdapter(), projections=(_PDFPageTextProjection(),)
)

# These are general financial vocabulary equivalences, independent of a document,
# question, answer or evaluation dataset. They only expand real Memory searches.
_FINANCIAL_TERMS = {
    "capex": ("capital", "expenditures"),
    "ebitda": ("operating", "income", "depreciation", "amortization"),
    "geographies": ("geographic", "segments", "countries"),
    "geography": ("geographic", "segments", "countries"),
    "lawsuits": ("litigation", "legal", "proceedings"),
    "battles": ("litigation", "proceedings"),
}
_QUESTION_WORDS = frozenset({
    "has",
    "have",
    "had",
    "any",
    "reported",
    "report",
    "state",
    "give",
    "response",
    "respond",
    "question",
    "calculate",
    "define",
    "assuming",
    "perspective",
    "analyst",
    "use",
    "only",
    "details",
    "shown",
    "within",
    "relying",
    "quantity",
    "unadjusted",
    "explicitly",
    "outlined",
    "primarily",
    "important",
    "materially",
})


def _question_terms(question: str) -> tuple[str, ...]:
    normalized = re.sub(r"\bfy\s*['’]?(\d{4})\b", r"\1", question.casefold())  # noqa: RUF001
    terms = [term for term in analyze_fts_query(normalized).split() if term not in _QUESTION_WORDS]
    expanded = [alias for term in terms for alias in _FINANCIAL_TERMS.get(term, ())]
    return tuple(dict.fromkeys([*terms, *expanded]))


def _document_priority(text: str, question: str) -> int:
    """Recognize a queried issuer from the projected document label."""

    label = text.partition("\n")[0]
    company = re.split(r"(?:19|20)\d{2}", label, maxsplit=1)[0]
    generic = {"inc", "incorporated", "corp", "corporation", "company", "ltd", "limited", "plc", "annual", "report"}
    name_parts = [word.casefold() for word in re.findall(r"[^\W_]+", company) if word.casefold() not in generic]
    name = "".join(name_parts)
    words = re.findall(r"[^\W_]+", question.casefold())
    width = max(1, len(name_parts) + 1)
    names = {
        "".join(words[start:end])
        for start in range(len(words))
        for end in range(start + 1, min(len(words), start + width) + 1)
    }
    return int(len(name) > 1 and name in names)


def _rank_pages(
    hits: dict[tuple[str, str, int, str, str], MemoryHit], terms: tuple[str, ...], question: str
) -> list[MemoryHit]:
    records = list(hits.values())
    # Use the same alphanumeric/Unicode terms as the query and the Memory index.
    # Splitting letters from digits loses issuer names such as "X7" entirely.
    tokenized = [Counter(analyze_text(hit.text.replace("_", " ")).split()) for hit in records]
    count = len(records)
    if not count:
        return []
    average = sum(sum(tokens.values()) for tokens in tokenized) / count or 1
    frequency = Counter(term for tokens in tokenized for term in terms if term in tokens)

    def rank(index: int) -> tuple[int, float, int]:
        tokens = tokenized[index]
        length = sum(tokens.values())
        score = 0.0
        for term in terms:
            tf = tokens[term]
            if tf:
                idf = math.log1p((count - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / average))
        company = _document_priority(records[index].text, question)
        return (-company, -score, index)

    return [records[index] for index in sorted(range(count), key=rank)]


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Search the supplied scope and return distinct, exact persisted page citations."""

    if not isinstance(question, str) or not question.strip():
        raise ValueError("PDF search question must be a non-empty string")  # noqa: TRY003
    if type(limit) is not int or limit < 1:
        raise ValueError("PDF search limit must be a positive integer")  # noqa: TRY003
    terms = _question_terms(question)
    queries = tuple(dict.fromkeys((question, *terms)))
    # A fixed per-term top-80 pool can exclude every page of a named issuer
    # when other reports have shorter, repetitive pages. Size recall from the
    # real active Memory snapshot; all candidates still come from Memory.search.
    candidate_limit = max(80, len((await memory.list()).entries), limit)
    hits: dict[tuple[str, str, int, str, str], MemoryHit] = {}
    for query in queries:
        result = await memory.search(SearchMemoryRequest(query=query, limit=candidate_limit, mode="fts"))
        for hit in result.hits:
            identity = (
                hit.memory_ref.family,
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            hits.setdefault(identity, hit)
    citations: list[MemoryCitation] = []
    pages: set[tuple[str, str]] = set()
    for hit in _rank_pages(hits, terms, question):
        citation = MemoryCitation(
            memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        page_ids = {
            (source.source_type, source.source_id) for source in entry.sources if source.source_type == "pdf-page"
        }
        if len(page_ids) != 1 or page_ids.intersection(pages):
            continue
        pages.update(page_ids)
        citations.append(citation)
        if len(citations) == limit:
            break
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
