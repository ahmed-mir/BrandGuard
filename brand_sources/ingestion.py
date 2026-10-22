"""Bounded, deterministic text extraction for private brand-source uploads."""
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath
import re
import zipfile

from django.conf import settings
from docx import Document as DocxDocument
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader


class DocumentImportError(Exception):
    """Safe, user-facing validation error for an uploaded document."""


@dataclass(frozen=True)
class ExtractedDocument:
    document_type: str
    text: str
    page_count: int | None = None
    raw_bytes: bytes = b""


SUPPORTED_TYPES = {".txt": "TXT", ".docx": "DOCX", ".pdf": "PDF"}
MAX_DOCX_MEMBERS = 2_048
MAX_DOCX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_DOCX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 200


def extract_uploaded_document(upload):
    """Read a bounded upload, validate its actual structure, and return text."""
    if upload.size > settings.BRAND_DOCUMENT_MAX_UPLOAD_BYTES:
        raise DocumentImportError("The document exceeds the 5 MB upload limit.")
    upload.seek(0)
    raw = upload.read(settings.BRAND_DOCUMENT_MAX_UPLOAD_BYTES + 1)
    upload.seek(0)
    if len(raw) > settings.BRAND_DOCUMENT_MAX_UPLOAD_BYTES:
        raise DocumentImportError("The document exceeds the 5 MB upload limit.")

    suffix = PurePath(upload.name or "").suffix.lower()
    document_type = SUPPORTED_TYPES.get(suffix)
    if document_type is None:
        raise DocumentImportError("Choose a .txt, .docx, or text-based .pdf file.")

    if document_type == "TXT":
        text = _extract_txt(raw)
        page_count = None
    elif document_type == "DOCX":
        text = _extract_docx(raw)
        page_count = None
    else:
        text, page_count = _extract_pdf(raw)

    text = normalize_extracted_text(text)
    if not text or not any(character.isalnum() for character in text):
        raise DocumentImportError(_no_text_message(document_type))
    if len(text) > settings.BRAND_DOCUMENT_MAX_EXTRACTED_CHARS:
        raise DocumentImportError(
            f"The extracted text exceeds the {settings.BRAND_DOCUMENT_MAX_EXTRACTED_CHARS:,}-character limit. "
            "Shorten the document or enter the relevant text manually."
        )
    return ExtractedDocument(document_type=document_type, text=text, page_count=page_count, raw_bytes=raw)


def normalize_extracted_text(text):
    """Normalize line endings and redundant blank lines without rewriting prose."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    lines = [line.rstrip() for line in text.split("\n")]
    output = []
    blank_lines = 0
    for line in lines:
        if line.strip():
            output.append(line)
            blank_lines = 0
        elif output and blank_lines == 0:
            output.append("")
            blank_lines = 1
    return "\n".join(output).strip()


def _extract_txt(raw):
    if raw.startswith(b"%PDF-") or raw.startswith(b"PK\x03\x04") or raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise DocumentImportError("The file contents do not match its .txt extension. Choose the correct original document.")
    if b"\x00" in raw:
        raise DocumentImportError("This TXT file contains binary data. Save a plain-text UTF-8 file and try again.")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentImportError("This TXT file is not valid UTF-8. Save it as UTF-8 and try again.") from exc


def _extract_docx(raw):
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_DOCX_MEMBERS:
                raise DocumentImportError("This DOCX contains too many internal parts to process safely.")
            if "[Content_Types].xml" not in archive.namelist() or "word/document.xml" not in archive.namelist():
                raise DocumentImportError("This file is not a valid DOCX document.")
            total_uncompressed = 0
            for info in infos:
                if info.flag_bits & 0x1:
                    raise DocumentImportError("Encrypted DOCX files are not supported. Remove document protection and try again.")
                if info.file_size > MAX_DOCX_MEMBER_BYTES:
                    raise DocumentImportError("A DOCX component exceeds the safe processing limit.")
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise DocumentImportError("The decompressed DOCX content exceeds the safe processing limit.")
                if info.file_size > 1_000_000 and info.compress_size and info.file_size / info.compress_size > MAX_DOCX_COMPRESSION_RATIO:
                    raise DocumentImportError("This DOCX uses an unsafe compression ratio and cannot be processed.")
            if archive.testzip() is not None:
                raise DocumentImportError("This DOCX archive is damaged and cannot be opened.")

        document = DocxDocument(BytesIO(raw))
        blocks = []
        body = document.element.body
        for child in body.iterchildren():
            if isinstance(child, CT_P):
                paragraph = Paragraph(child, document)
                if paragraph.text.strip():
                    blocks.append(paragraph.text)
            elif isinstance(child, CT_Tbl):
                table = Table(child, document)
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        blocks.append("\t".join(cells))
        return "\n".join(blocks)
    except DocumentImportError:
        raise
    except Exception as exc:
        raise DocumentImportError("This DOCX is corrupted or malformed and cannot be read.") from exc


def _extract_pdf(raw):
    if b"%PDF-" not in raw[:1024]:
        raise DocumentImportError("The file extension is PDF, but its contents are not a PDF document.")
    try:
        reader = PdfReader(BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise DocumentImportError("Password-protected PDFs are not supported. Remove the password and try again.")
        page_count = len(reader.pages)
        if page_count > settings.BRAND_DOCUMENT_MAX_PDF_PAGES:
            raise DocumentImportError(
                f"This PDF has {page_count} pages; the limit is {settings.BRAND_DOCUMENT_MAX_PDF_PAGES}."
            )
        pages = [page.extract_text() or "" for page in reader.pages]
    except DocumentImportError:
        raise
    except Exception as exc:
        raise DocumentImportError("This PDF is corrupted or malformed and cannot be read.") from exc
    return "\n\n".join(pages), page_count


def _no_text_message(document_type):
    if document_type == "PDF":
        return (
            "No readable text was found in this PDF. Scanned PDFs and image OCR are not supported; "
            "enter the authoritative text manually instead."
        )
    return "No meaningful text was found in this document. Check the file or enter the authoritative text manually."
