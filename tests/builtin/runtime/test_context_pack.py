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

"""Wire validation and transport-cost regressions for independent clients."""

import json

import pytest

from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)


@pytest.mark.parametrize("chunk_bytes", [1, 2, 3, 4, 7, 512])
def test_utf8_chunking_preserves_every_character_or_rejects_small_limit(chunk_bytes):
    text = "Leading\n海洋🌊 é \x00 trailing"
    if chunk_bytes < 4:
        with pytest.raises(ContextPackError):
            ContextPack.from_content(text, chunk_bytes=chunk_bytes)
        return
    pack = ContextPack.from_content(text, chunk_bytes=chunk_bytes)
    assert b"".join(pack.items).decode() == text
    assert all(len(item) <= chunk_bytes and item.decode() for item in pack.items)
    assert ContextPack.from_bytes(pack.to_bytes()) == pack


@pytest.mark.parametrize(
    "change",
    [
        {"byte_count": True},
        {"byte_count": 0},
        {"items": ["/w=="]},
        {"items": ["YQ="]},
        {"items": ["YR=="]},
        {"extra": 1},
        {"items": "YQ=="},
    ],
)
def test_pack_rejects_invalid_wire_fields(change):
    wire = {**json.loads(ContextPack.build((b"a",)).to_bytes()), **change}
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps(wire).encode())


@pytest.mark.parametrize("wire", [b"[]", b"null", b"{", b'"text"', b"\xff", b'{"schema":0,"schema":1}'])
def test_nonobject_duplicate_or_malformed_json_is_protocol_error(wire):
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(wire)
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(wire)


def test_steady_context_saves_actual_protocol_bytes_and_restores_empty():
    """Charges the initial full pack and every protocol byte, rather than payload alone."""
    content = "Stable evidence with exact provenance.\n" * 120
    first = ContextPack.from_content(content)
    total = first.byte_count
    base = ContextPack.from_bytes(first.to_bytes())
    for _ in range(39):
        delta = ContextPackDelta.from_bytes(ContextPackDelta.between(base, first).to_bytes())
        total += delta.byte_count
        base = apply_context_pack_delta(base, delta)
    assert 1 - total / (40 * len(content.encode())) >= 0.45
    empty = ContextPack.build(())
    assert apply_context_pack_delta(base, ContextPackDelta.between(base, empty)) == empty
