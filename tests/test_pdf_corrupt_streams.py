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

"""Damaged compressed page text cannot silently become a blank page."""

from __future__ import annotations

import base64
import io
import zlib

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NullObject,
    NumberObject,
)

from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document

_FIRST = b"BT /F1 12 Tf 20 100 Td (First page) Tj ET"
_SECOND = b"BT /F1 12 Tf 20 100 Td (Second page) Tj ET"
_CMAP = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /TextEvidence def
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
"""


def _stream(data: bytes, *, chain: bool = False) -> EncodedStreamObject:
    stream = EncodedStreamObject()
    stream._data = base64.a85encode(data, adobe=True) if chain else data
    stream[NameObject("/Filter")] = (
        ArrayObject((NameObject("/ASCII85Decode"), NameObject("/FlateDecode"))) if chain else NameObject("/FlateDecode")
    )
    return stream


def _pdf(mode: str, data: bytes, *, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    first = writer.add_blank_page(width=300, height=300)
    first[NameObject("/Resources")] = resources
    plain = DecodedStreamObject()
    plain.set_data(_FIRST)
    first[NameObject("/Contents")] = writer._add_object(plain)
    second = writer.add_blank_page(width=300, height=300)
    second[NameObject("/Resources")] = resources
    stream = _stream(data, chain=mode == "chain")
    if mode == "form":
        stream.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(NumberObject(value) for value in (0, 0, 300, 300)),
            NameObject("/Resources"): resources,
        })
        second[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({NameObject("/Form0"): writer._add_object(stream)})
        })
        contents = DecodedStreamObject()
        contents.set_data(b"/Form0 Do")
        second[NameObject("/Contents")] = writer._add_object(contents)
    elif mode in {"array", "invalid_array"}:
        second[NameObject("/Contents")] = ArrayObject((
            writer._add_object(stream),
            NumberObject(7) if mode == "invalid_array" else writer._add_object(DecodedStreamObject()),
        ))
    else:
        second[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt(user_password="", owner_password="owner")  # noqa: S106 - Disposable fixture.
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("mode", ["plain", "chain", "array", "form"])
@pytest.mark.parametrize("data", [b"not a zlib stream", zlib.compress(_SECOND)[:12]])
def test_unreadable_later_page_compression_fails_atomically(mode: str, data: bytes) -> None:
    raw = _pdf(mode, data)
    assert PdfReader(io.BytesIO(raw), strict=True).pages[0].extract_text() == "First page"
    with pytest.raises(ValueError, match="readable in full"):
        parse_pdf_document(PDFDocumentInput(document_id="BROKEN", content=raw))


def test_invalid_contents_array_item_is_not_discarded() -> None:
    with pytest.raises(ValueError, match="readable in full"):
        parse_pdf_document(
            PDFDocumentInput(document_id="BROKEN", content=_pdf("invalid_array", zlib.compress(_SECOND)))
        )


@pytest.mark.parametrize("mode", ["plain", "chain", "array", "form"])
@pytest.mark.parametrize("encrypted", [False, True])
def test_valid_compressed_contents_and_empty_password_remain_readable(mode: str, encrypted: bool) -> None:
    pages = parse_pdf_document(
        PDFDocumentInput(document_id="VALID", content=_pdf(mode, zlib.compress(_SECOND), encrypted=encrypted))
    )
    assert [page.page_number for page in pages] == [1, 2]
    assert pages[0].text == "First page"
    assert pages[1].text == "Second page"


@pytest.mark.parametrize("mode", ["plain", "chain", "array", "form"])
def test_legitimate_empty_compressed_stream_is_a_blank_page(mode: str) -> None:
    pages = parse_pdf_document(PDFDocumentInput(document_id="EMPTY", content=_pdf(mode, zlib.compress(b""))))
    assert pages[0].text == "First page"
    assert pages[1].text == ""


def _unicode_cmap_pdf(data: bytes, *, mode: str = "flate") -> bytes:
    text = b"Second page \x80 \x81"
    raw = _pdf("plain", zlib.compress(f"BT /F1 12 Tf 20 100 Td <{text.hex()}> Tj ET".encode("ascii")))
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(raw)))
    resources = writer.pages[1]["/Resources"]
    assert isinstance(resources, DictionaryObject)
    fonts = resources["/Font"]
    assert isinstance(fonts, DictionaryObject)
    font = fonts["/F1"]
    assert isinstance(font, DictionaryObject)
    if mode in {"name", "null"}:
        font[NameObject("/ToUnicode")] = NameObject("/Identity-H") if mode == "name" else NullObject()
    else:
        if mode == "plain":
            mapping = DecodedStreamObject()
            mapping.set_data(data)
        else:
            mapping = _stream(data, chain=mode == "chain")
        font[NameObject("/ToUnicode")] = writer._add_object(mapping)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("mode", ["flate", "chain"])
@pytest.mark.parametrize("data", [b"invalid compressed character map", zlib.compress(_CMAP)[:15]])
def test_unreadable_unicode_character_map_fails_atomically(mode: str, data: bytes) -> None:
    with pytest.raises(ValueError, match="readable in full"):
        parse_pdf_document(PDFDocumentInput(document_id="BROKEN_CMAP", content=_unicode_cmap_pdf(data, mode=mode)))


@pytest.mark.parametrize("mode", ["plain", "flate", "chain"])
def test_readable_character_maps_preserve_unicode_glyphs(mode: str) -> None:
    data = _CMAP if mode == "plain" else zlib.compress(_CMAP)
    pages = parse_pdf_document(PDFDocumentInput(document_id="CMAP", content=_unicode_cmap_pdf(data, mode=mode)))
    assert pages[0].text == "First page"
    assert pages[1].text == "Second page é ﬁ"


@pytest.mark.parametrize("mode", ["name", "null"])
def test_nonstream_character_map_values_keep_the_library_behavior(mode: str) -> None:
    raw = _unicode_cmap_pdf(b"", mode=mode)
    expected = PdfReader(io.BytesIO(raw), strict=True).pages[1].extract_text()
    pages = parse_pdf_document(PDFDocumentInput(document_id="CMAP", content=raw))
    assert pages[1].text == expected
