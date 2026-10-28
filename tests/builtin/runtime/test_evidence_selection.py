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

"""Evidence provenance and exact full-envelope budget regressions."""

import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import PreparedContextBuilder, PreparedMemoryCandidates


def _candidates(text: str, *, scope: str = "local", revision: int = 3):
    ref = ArtifactRef(family="memory", artifact_id="memory", revision=revision)
    return PreparedMemoryCandidates(
        scope_id=scope,
        memory_ref=ref,
        hits=(
            MemoryHit(
                memory_ref=ref, entry_id="entry", entry_version_id="version", text=text, score=1, matched_by=("fts",)
            ),
        ),
    )


def _build(query, candidates, max_bytes=1400, **options):
    return PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query=query, max_bytes=max_bytes, evidence_selection=True, **options),
        current_scope_id="local",
        memory_candidates=candidates,
    )


def test_same_text_in_distinct_scopes_retains_distinct_exact_citations():
    text = "Cobalt regulates sensor calibration."
    result = _build("cobalt", (_candidates(text), _candidates(text, scope="other", revision=7)), max_bytes=2000)
    assert {fragment.address.memory.scope_id for fragment in result.fragments} == {"local", "other"}
    assert {fragment.address.memory.artifact.revision for fragment in result.fragments} == {3, 7}
    assert result.origins == tuple(fragment.address for fragment in result.fragments)
    items = json.loads(result.context.content.splitlines()[-2])["items"]
    assert len(items) == len(result.fragments) == 2
    for item, fragment in zip(items, result.fragments, strict=True):
        assert item["citation"]["scope_id"] == fragment.address.memory.scope_id
        assert item["content"] == text[fragment.start : fragment.end]


def test_disjoint_late_evidence_and_query_switch_use_literal_coordinates():
    first = "The cobalt sensor tolerates cold."
    second = "The cobalt sensor fails above 37 degrees."
    filler = "General administrative history. " * 60
    text = first + " " + filler + second
    result = _build("cobalt sensor", (_candidates(text),), max_bytes=1600)
    assert any(first in fragment.text for fragment in result.fragments)
    assert any(second in fragment.text for fragment in result.fragments)
    for fragment in result.fragments:
        assert fragment.text == text[fragment.start : fragment.end]
    changed = _build("administrative history", (_candidates(text),), max_bytes=900)
    assert changed.fragments != result.fragments


@pytest.mark.parametrize("max_bytes", [512, 600, 800, 1200, 1400, 1800])
def test_escaped_unicode_and_long_single_sentence_obey_the_entire_budget(max_bytes):
    text = 'Unknown "quoted" history \\ records ' * 100 + "海洋观测 🌊 sensor safety at 37 degrees " * 20
    result = _build("海洋观测 sensor safety", (_candidates(text),), max_bytes=max_bytes)
    assert result.context.content_bytes == len((result.context.content or "").encode()) <= max_bytes
    for fragment in result.fragments:
        assert type(fragment.start) is int and type(fragment.end) is int
        assert 0 <= fragment.start < fragment.end <= len(text)
        assert fragment.text == text[fragment.start : fragment.end]
    if result.fragments:
        assert "sensor" in "".join(fragment.text for fragment in result.fragments)
        assert "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1" in result.context.content


def test_disabled_mode_is_identical_to_historical_prepare_and_has_no_fragments():
    candidates = (_candidates("A station records the temperature."),)
    builder = PreparedContextBuilder()
    request = PrepareContextRequest(query="station", max_bytes=1400)
    left = builder.build_scopes_result(request=request, current_scope_id="local", memory_candidates=candidates)
    right = builder.build_scopes_result(
        request=request.model_copy(update={"evidence_selection": False}),
        current_scope_id="local",
        memory_candidates=candidates,
    )
    assert left == right and not left.fragments
    assert not PrepareContextRequest(query="station").evidence_selection
    with pytest.raises(ValidationError):
        PrepareContextRequest.model_validate({"query": "station", "evidence_selection": "false"})


def test_empty_candidates_and_budget_probe_agree_with_prepared_selection():
    assert _build("unknown", ()).context.content is None
    candidates = (_candidates("海洋观测 sensor calibration. " * 40),)
    request = PrepareContextRequest(query="sensor", max_bytes=1000, evidence_selection=True)
    builder = PreparedContextBuilder()
    result = builder.build_scopes_result(request=request, current_scope_id="local", memory_candidates=candidates)
    budget = builder.probe_budget(request=request, current_scope_id="local", memory_candidates=candidates)
    assert budget.delivered_items == len(result.origins)
    assert budget.unused_bytes == request.max_bytes - result.context.content_bytes
