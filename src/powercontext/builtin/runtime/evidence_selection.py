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

"""Query-driven literal sentence selection, charged against the complete wire text."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PreparedContext
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

if TYPE_CHECKING:
    from powercontext.builtin.runtime.models import PrepareContextRequest
    from powercontext.builtin.runtime.prepared_context import (
        MemoryEntryAddress,
        MemoryEvidenceFragment,
        PreparedContextBuild,
        PreparedMemoryCandidates,
    )

_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_BOUNDARIES = re.compile(r"[.!?](?:[\"'”’)]*)(?=\s|$)|[。！？]|\n+")  # noqa: RUF001
_STOP = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "their",
    "we",
    "our",
    "you",
    "your",
    "i",
    "for",
    "which",
    "who",
    "what",
    "how",
    "does",
    "do",
    "did",
    "can",
    "could",
    "would",
    "should",
    "than",
    "then",
    "into",
    "through",
    "during",
    "among",
    "has",
    "have",
    "had",
])


def _stem(word: str) -> str:
    word = word.casefold()
    if not word.isascii() or len(word) <= 3:
        return word
    for suffix, replacement in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)] + replacement
    return word


def _terms(text: str) -> Counter[str]:
    terms: Counter[str] = Counter()
    for match in _WORDS.finditer(text):
        word = match.group().casefold()
        if word not in _STOP:
            terms[_stem(word)] += 1
            # Bigrams let a Chinese query match a phrase within longer unspaced text.
            if any("\u3400" <= char <= "\u9fff" for char in word):
                terms.update(word[index : index + 2] for index in range(len(word) - 1))
    return terms


@dataclass(frozen=True)
class _Document:
    address: MemoryEntryAddress
    text: str
    terms: Counter[str]


@dataclass(frozen=True)
class _Span:
    document: int
    start: int
    end: int
    score: float


def _intervals(text: str) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for boundary in _BOUNDARIES.finditer(text):
        end = boundary.end()
        left = start
        while left < end and text[left].isspace():
            left += 1
        right = end
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            spans.append((left, right))
        start = end
    while start < len(text) and text[start].isspace():
        start += 1
    end = len(text.rstrip())
    if start < end:
        spans.append((start, end))
    return spans


def _documents(candidates: Sequence[PreparedMemoryCandidates]) -> list[_Document]:
    from powercontext.builtin.runtime.prepared_context import MemoryEntryAddress

    documents = []
    seen = set()
    for group in candidates:
        for hit in group.hits:
            if hit.memory_ref != group.memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")
            if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                continue
            address = MemoryEntryAddress(
                memory=ArtifactAddress(scope_id=group.scope_id, artifact=hit.memory_ref),
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            identity = (
                group.scope_id,
                hit.memory_ref.family,
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            if identity in seen:
                continue
            seen.add(identity)
            documents.append(_Document(address, hit.text, _terms(hit.text)))
    return documents


def _rank_spans(documents: Sequence[_Document], query: str) -> list[_Span]:
    terms = _terms(query)
    frequency = Counter(term for document in documents for term in document.terms)
    weights = {term: 1 + math.log((len(documents) + 1) / (frequency[term] + 0.5)) for term in terms}
    total_weight = sum(weights.values()) or 1
    spans = []
    for index, document in enumerate(documents):
        coverage = sum(weight for term, weight in weights.items() if term in document.terms) / total_weight
        for start, end in _intervals(document.text):
            sentence = _terms(document.text[start:end])
            overlap = sum(weight * min(sentence[term], 2) for term, weight in weights.items() if term in sentence)
            score = overlap * (1 + coverage) + 0.15 * coverage
            spans.append(_Span(index, start, end, score))
    if spans and not any(span.score for span in spans):
        # Unknown vocabulary may still have been recalled semantically. Preserve a
        # literal candidate instead of inventing evidence or suppressing every hit.
        return [_Span(span.document, span.start, span.end, 1 / (1 + span.document)) for span in spans]
    return spans


def _merge(spans: Sequence[_Span], documents: Sequence[_Document]) -> tuple[_Span, ...]:
    merged: list[_Span] = []
    for span in sorted(spans, key=lambda value: (value.document, value.start, value.end)):
        if merged:
            previous = merged[-1]
            same = previous.document == span.document
            if same and (
                span.start <= previous.end or not documents[span.document].text[previous.end : span.start].strip()
            ):
                merged[-1] = _Span(
                    span.document, previous.start, max(previous.end, span.end), previous.score + span.score
                )
                continue
        merged.append(span)
    return tuple(merged)


def _fragments(spans: Sequence[_Span], documents: Sequence[_Document]) -> tuple[MemoryEvidenceFragment, ...]:
    from powercontext.builtin.runtime.prepared_context import MemoryEvidenceFragment

    fragments = []
    for span in _merge(spans, documents):
        document = documents[span.document]
        fragments.append(
            MemoryEvidenceFragment(document.address, span.start, span.end, document.text[span.start : span.end])
        )
    return tuple(fragments)


def _render(fragments: Sequence[MemoryEvidenceFragment]) -> str:
    items = [
        {
            "citation": {
                "scope_id": fragment.address.memory.scope_id,
                "memory_ref": fragment.address.memory.artifact.model_dump(mode="json"),
                "entry_id": fragment.address.entry_id,
                "entry_version_id": fragment.address.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }
        for fragment in fragments
    ]
    body = json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
    return f"{TRUST_POLICY}\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n{body}\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1"


def _fit_long_span(span: _Span, documents: Sequence[_Document], query: str, max_bytes: int) -> _Span | None:
    """When one sentence cannot fit, retain a literal window around its strongest query match."""
    text = documents[span.document].text
    query_terms = _terms(query)
    matches = [match for match in _WORDS.finditer(text, span.start, span.end) if _stem(match.group()) in query_terms]
    start = matches[0].start() if matches else span.start
    best = None
    lower, upper = matches[0].end() if matches else start + 1, span.end
    while lower <= upper:
        end = (lower + upper) // 2
        candidate = _Span(span.document, start, end, span.score)
        if len(_render(_fragments((candidate,), documents)).encode("utf-8")) <= max_bytes:
            best = candidate
            lower = end + 1
        else:
            upper = end - 1
    return best


def _best_addition(
    pending: Sequence[_Span], selected: Sequence[_Span], documents: Sequence[_Document], content: str, max_bytes: int
) -> tuple[int, _Span, str] | None:
    best: tuple[float, int, _Span, str] | None = None
    for index, span in enumerate(pending):
        if span.score <= 0:
            continue
        text = _render(_fragments((*selected, span), documents))
        size = len(text.encode("utf-8"))
        if size > max_bytes:
            continue
        marginal = max(1, size - len(content.encode("utf-8")))
        priority = span.score / math.sqrt(marginal)
        if best is None or priority > best[0]:
            best = priority, index, span, text
    return None if best is None else best[1:]


def select_memory_evidence(
    request: PrepareContextRequest, candidates: Sequence[PreparedMemoryCandidates]
) -> PreparedContextBuild:
    """Select, merge, and serialize exact Memory spans within the full UTF-8 budget."""
    from powercontext.builtin.runtime.prepared_context import PreparedContextBuild

    documents = _documents(candidates)
    pending = _rank_spans(documents, request.query)
    selected: list[_Span] = []
    content = ""
    while pending:
        best = _best_addition(pending, selected, documents, content, request.max_bytes)
        if best is None:
            break
        index, span, content = best
        selected.append(span)
        pending.pop(index)
    if not selected and pending:
        for span in sorted(pending, key=lambda value: -value.score):
            fitted = _fit_long_span(span, documents, request.query, request.max_bytes)
            if fitted is not None:
                selected.append(fitted)
                break
    if not selected:
        return PreparedContextBuild(context=PreparedContext(status="empty", content=None, content_bytes=0), origins=())
    fragments = _fragments(selected, documents)
    content = _render(fragments)
    return PreparedContextBuild(
        context=PreparedContext(status="ready", content=content, content_bytes=len(content.encode("utf-8"))),
        origins=tuple(fragment.address for fragment in fragments),
        fragments=fragments,
    )
