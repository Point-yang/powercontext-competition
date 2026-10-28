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

"""Self-contained, canonical context packs and prefix-reuse deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ContextPackError(ValueError):
    """The context pack, delta, or UTF-8 chunk is invalid."""


def _encode(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContextPackError("duplicate-field")
        value[key] = item
    return value


def _decode(data: bytes, fields: set[str], schema: str) -> dict[str, Any]:
    try:
        if not isinstance(data, bytes):
            raise ContextPackError("wire-must-be-bytes")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ContextPackError("invalid-json") from error
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != schema:
        raise ContextPackError("invalid-fields-or-schema")
    return value


def _validate_items(items: tuple[bytes, ...]) -> None:
    if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("items-must-be-bytes-tuple")
    try:
        for item in items:
            item.decode("utf-8")
    except UnicodeError as error:
        raise ContextPackError("invalid-utf8-chunk") from error


def _encoded_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decoded_items(values: object) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError("items-must-be-array")
    items: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError("chunk-must-be-base64-string")
        try:
            item = base64.b64decode(value.encode("ascii"), validate=True)
        except (UnicodeError, ValueError, binascii.Error) as error:
            raise ContextPackError("invalid-base64") from error
        if base64.b64encode(item).decode("ascii") != value:
            raise ContextPackError("noncanonical-base64")
        items.append(item)
    result = tuple(items)
    _validate_items(result)
    return result


def _validate_digest(value: object) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ContextPackError("invalid-digest")


def _pack_fields(items: tuple[bytes, ...]) -> dict[str, Any]:
    unsigned = {"schema": _PACK_SCHEMA, "items": _encoded_items(items)}
    fields = {**unsigned, "digest": "sha256:" + hashlib.sha256(_encode(unsigned)).hexdigest(), "byte_count": 0}
    while fields["byte_count"] != len(_encode(fields)):
        fields["byte_count"] = len(_encode(fields))
    return fields


@dataclass(frozen=True)
class ContextPack:
    """An immutable sequence of exact UTF-8 chunks with a canonical wire identity."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    def __post_init__(self) -> None:
        _validate_items(self.items)
        expected = _pack_fields(self.items)
        if self.digest != expected["digest"]:
            raise ContextPackError("pack-digest-mismatch")
        if type(self.byte_count) is not int or self.byte_count != expected["byte_count"]:
            raise ContextPackError("pack-byte-count-mismatch")

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        _validate_items(items)
        fields = _pack_fields(items)
        return cls(items=items, digest=fields["digest"], byte_count=fields["byte_count"])

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = 512) -> ContextPack:
        if type(chunk_bytes) is not int or chunk_bytes <= 0 or not isinstance(content, str):
            raise ContextPackError("invalid-content-or-chunk-limit")
        try:
            encoded = content.encode("utf-8")
        except UnicodeError as error:
            raise ContextPackError("invalid-content-utf8") from error
        items = []
        start = 0
        while start < len(encoded):
            end = min(start + chunk_bytes, len(encoded))
            while end < len(encoded) and end > start and encoded[end] & 0xC0 == 0x80:
                end -= 1
            if end == start:
                raise ContextPackError("chunk-limit-smaller-than-codepoint")
            items.append(encoded[start:end])
            start = end
        return cls.build(tuple(items))

    def to_bytes(self) -> bytes:
        return _encode(_pack_fields(self.items))

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        value = _decode(data, {"schema", "items", "digest", "byte_count"}, _PACK_SCHEMA)
        return cls(_decoded_items(value["items"]), value["digest"], value["byte_count"])


@dataclass(frozen=True)
class ContextPackDelta:
    """Retain a base prefix and replace its remaining chunks."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_digest(self.base_digest)
        _validate_digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("invalid-prefix")
        _validate_items(self.appended)

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("invalid-pack")
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _encode({
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encoded_items(self.appended),
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        value = _decode(data, {"schema", "base_digest", "target_digest", "prefix_items", "appended"}, _DELTA_SCHEMA)
        return cls(
            value["base_digest"], value["target_digest"], value["prefix_items"], _decoded_items(value["appended"])
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Validate identities and reconstruct a target using only the given wire state."""
    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("invalid-pack-or-delta")
    if base.digest != delta.base_digest:
        raise ContextPackError("base-digest-mismatch")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("prefix-out-of-bounds")
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("target-digest-mismatch")
    return target
