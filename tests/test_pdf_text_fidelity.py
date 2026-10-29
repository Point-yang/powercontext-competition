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

"""Preserve words when a PDF positions the letters of a word separately."""

from __future__ import annotations

import io
import re
from collections import Counter

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document


def _positioned_text_pdf(*, rotated: bool = False, letter_spacing: bool = True) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=1000, height=400)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Courier"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    text = "Revenue increased through international operations"
    if letter_spacing:
        positioned = " ".join(f"({character}) -300" for character in text)
        body = f"BT /F1 12 Tf 20 350 Td [{positioned}] TJ ET\n"
    else:
        body = "BT /F1 12 Tf 20 350 Td (Class A B C D E F G H I J K L M N O P and values 1 2 3) Tj ET\n"
    if rotated:
        body += "BT /F1 12 Tf 0 1 -1 0 900 60 Tm (Rotated revenue 240) Tj ET\n"
    stream = DecodedStreamObject()
    stream.set_data(body.encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def _terms(text: str) -> Counter[str]:
    return Counter(re.findall(r"[a-z]+|\d+", text.lower()))


def test_individually_positioned_letters_remain_complete_words() -> None:
    pages = parse_pdf_document(PDFDocumentInput(document_id="positioned", content=_positioned_text_pdf()))
    assert _terms(pages[0].text) == _terms("Revenue increased through international operations")


def test_recovering_positioned_words_keeps_rotated_text_on_the_same_page() -> None:
    pages = parse_pdf_document(PDFDocumentInput(document_id="positioned", content=_positioned_text_pdf(rotated=True)))
    assert _terms(pages[0].text) == _terms("Revenue increased through international operations Rotated revenue 240")


def test_separate_letter_labels_and_numeric_columns_are_not_combined() -> None:
    pages = parse_pdf_document(
        PDFDocumentInput(document_id="table", content=_positioned_text_pdf(letter_spacing=False))
    )
    assert _terms(pages[0].text) == _terms("Class A B C D E F G H I J K L M N O P and values 1 2 3")
