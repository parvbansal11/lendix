"""Bounded, non-executing parsing for untrusted application attachments."""

from __future__ import annotations

import csv
import io
import json
import warnings
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_EXPANDED_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_RECORDS = 1000
MAX_COLUMNS = 100
ALLOWED_EXTENSIONS = {"csv", "json", "pdf", "png", "jpg", "jpeg", "docx", "xlsx"}
MIME_TYPES = {
    "csv": "text/csv", "json": "application/json", "pdf": "application/pdf", "png": "image/png",
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _bounded_text(value: str) -> str:
    if len(value) > MAX_TEXT_CHARS:
        raise ValueError("Extracted text exceeds the 100,000-character limit. Split the upload into smaller files.")
    return value


def _utf8(content: bytes) -> str:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV and JSON uploads must use UTF-8 text encoding.") from exc
    if "\x00" in text:
        raise ValueError("The text file contains binary data.")
    return _bounded_text(text)


def _check_archive(content: bytes, extension: str) -> None:
    if not content.startswith(b"PK\x03\x04"):
        raise ValueError("The file signature does not match its extension.")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                raise ValueError("The document expands beyond the safe parsing limit.")
            seen = set()
            for item in members:
                if item.filename in seen:
                    raise ValueError("The archive contains duplicate entries.")
                seen.add(item.filename)
                parts = item.filename.replace("\\", "/").split("/")
                if item.filename.startswith(("/", "\\")) or ".." in parts or item.flag_bits & 1:
                    raise ValueError("Unsafe or encrypted document archive.")
                if item.file_size > 1024 * 1024 and item.file_size / max(item.compress_size, 1) > 200:
                    raise ValueError("The document has an unsafe compression ratio.")
                lower = item.filename.lower()
                if lower.endswith(("vbaproject.bin", ".exe", ".dll", ".js")) or "/embeddings/" in lower:
                    raise ValueError("Active or embedded content is not accepted.")
            required = "word/document.xml" if extension == "docx" else "xl/workbook.xml"
            if "[Content_Types].xml" not in seen or required not in seen:
                raise ValueError("The document package does not match its extension.")
            for item in members:
                if item.filename.lower().endswith((".xml", ".rels")):
                    xml = archive.read(item)
                    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                        raise ValueError("XML entity declarations are not accepted.")
    except zipfile.BadZipFile as exc:
        raise ValueError("The document archive is damaged.") from exc


def _applications(value: Any) -> list[dict]:
    if isinstance(value, dict):
        if "applications" in value:
            value = value["applications"]
        elif "application" in value:
            value = [value["application"]]
        else:
            value = [value]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Applications must be a JSON object or a list of objects.")
    if len(value) > MAX_RECORDS:
        raise ValueError(f"Upload at most {MAX_RECORDS} applications per file.")
    if any(len(row) > MAX_COLUMNS for row in value):
        raise ValueError(f"An application may contain at most {MAX_COLUMNS} fields.")
    return value


def parse_upload(filename: str, content: bytes) -> dict:
    """Return extracted data, never execute it or treat its text as instructions."""
    if not isinstance(content, bytes):
        raise ValueError("Upload content must be bytes.")
    if not content:
        raise ValueError("The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("Uploads must be 5 MB or smaller.")
    safe_name = Path(filename.replace("\\", "/")).name
    extension = Path(safe_name).suffix.lower().lstrip(".")
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Allowed uploads: CSV, JSON, PDF, PNG, JPG, DOCX and XLSX.")
    result = {"filename": safe_name, "kind": extension, "content_type": MIME_TYPES[extension],
              "size_bytes": len(content), "text": "", "applications": [], "untrusted": True,
              "needs_officer_verification": extension not in {"csv", "json"}, "warnings": []}
    try:
        if extension == "json":
            text = _utf8(content)
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("JSON contains duplicate field names.")
                    result[key] = value
                return result
            parsed = json.loads(text, object_pairs_hook=unique_object,
                                parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON numbers are not allowed.")))
            result["applications"] = _applications(parsed)
            result["text"] = text
        elif extension == "csv":
            text = _utf8(content)
            reader = csv.DictReader(io.StringIO(text), strict=True)
            fields = reader.fieldnames
            if not fields or any(not field.strip() for field in fields) or len(set(fields)) != len(fields):
                raise ValueError("CSV requires unique, non-empty column headers.")
            if len(fields) > MAX_COLUMNS:
                raise ValueError(f"CSV may contain at most {MAX_COLUMNS} columns.")
            rows = []
            for row in reader:
                if None in row:
                    raise ValueError("A CSV row contains more values than its headers.")
                rows.append(row)
                if len(rows) > MAX_RECORDS:
                    raise ValueError(f"Upload at most {MAX_RECORDS} applications per file.")
            result["applications"] = rows
            result["text"] = text
        elif extension == "pdf":
            if not content.startswith(b"%PDF-"):
                raise ValueError("The file signature does not match its extension.")
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise ValueError("Password-protected PDFs are not supported.")
            if len(reader.pages) > 100:
                raise ValueError("Upload a PDF with 100 pages or fewer.")
            texts = []
            for page in reader.pages:
                stream = page.get_contents()
                if stream is not None and len(stream.get_data()) > MAX_EXPANDED_BYTES:
                    raise ValueError("PDF page content expands beyond the safe parsing limit.")
                texts.append(page.extract_text() or "")
                _bounded_text("\n".join(texts))
            result["text"] = "\n".join(texts)
            result["page_count"] = len(reader.pages)
            result["warnings"].append("Text extraction only. An officer must verify document details; no identity or income verification was performed.")
            if not result["text"].strip():
                result["warnings"].append("No readable text was found. Scanned PDFs require officer verification; OCR was not performed.")
        elif extension == "docx":
            _check_archive(content, extension)
            from docx import Document
            document = Document(io.BytesIO(content))
            blocks = [p.text for p in document.paragraphs]
            for table in document.tables:
                blocks.extend("\t".join(cell.text for cell in row.cells) for row in table.rows)
            result["text"] = _bounded_text("\n".join(blocks))
            result["warnings"].append("Text extraction only. An officer must verify the supplied document details.")
        elif extension == "xlsx":
            _check_archive(content, extension)
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True, keep_links=False)
            try:
                if len(workbook.worksheets) > 20:
                    raise ValueError("Upload a workbook with 20 sheets or fewer.")
                sheets = []
                total_rows = 0
                total_cells = 0
                for sheet in workbook.worksheets:
                    if (sheet.max_column or 0) > MAX_COLUMNS or (sheet.max_row or 0) > MAX_RECORDS + 1:
                        raise ValueError("Workbook dimensions exceed the parsing limit.")
                    values = []
                    for row in sheet.iter_rows(values_only=True):
                        total_rows += 1
                        total_cells += len(row)
                        if total_rows > MAX_RECORDS + 20 or total_cells > 50_000:
                            raise ValueError("Workbook contents exceed the parsing limit.")
                        values.append([value.isoformat() if isinstance(value, (date, datetime)) else value for value in row])
                    sheets.append({"name": sheet.title, "rows": values})
                result["sheets"] = sheets
                result["text"] = _bounded_text(json.dumps(sheets, ensure_ascii=False, default=str))
                result["warnings"].append("Read-only cell values. Formulas were not evaluated; cached formula values may be absent or stale.")
            finally:
                workbook.close()
        else:
            expected = b"\x89PNG\r\n\x1a\n" if extension == "png" else b"\xff\xd8\xff"
            if not content.startswith(expected):
                raise ValueError("The file signature does not match its extension.")
            from PIL import Image
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as picture:
                    width, height = picture.size
                    if width * height > 20_000_000:
                        raise ValueError("Images may contain at most 20 million pixels.")
                    expected_format = "PNG" if extension == "png" else "JPEG"
                    if picture.format != expected_format:
                        raise ValueError("The image type does not match its extension.")
                    result["image"] = {"width": width, "height": height, "format": picture.format}
                    picture.verify()
            result["warnings"].append("Image metadata only. OCR was not performed. An officer must verify the image and any identity or income information.")
        return result
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The uploaded file could not be parsed safely. Check its type and contents.") from exc
