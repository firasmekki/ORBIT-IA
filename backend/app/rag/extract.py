"""Extracts plain text from uploaded office files so it can flow through
the same chunking/embedding pipeline as hand-typed documents (app/rag/ingest.py).

The extracted text - not the original file - is what gets embedded and
searched. The original bytes are kept in MinIO (app/core/storage.py) purely
for download; they are never re-parsed at query time.

Also produces a page/section breakdown (`ExtractedPage`) used by
search_keyword and get_document_section for exact, addressable lookups -
see app/models/document.py's DocumentPage docstring for why this is a
separate structure from the overlapping RAG chunks.

Splitting strategy per format:
- PDF: one page per physical page (`page_no` set, never further split -
  page number is already a meaningful, real anchor). A page with no text
  layer (scanned/image page) falls back to OCR for that page only - a
  mixed PDF (some real text pages, some scanned) is handled correctly
  page-by-page, never OCR'd wholesale just because one page needs it.
- PPTX: one page per slide (`page_no` set, same anchor rationale as PDF) -
  text frames, table cells, then OCR text from any picture shape on that
  slide, in that order.
- Images (JPG/PNG): a single block, OCR'd - there is no sub-structure to
  anchor a page/section to.
- XLSX: one section per sheet (`section` = sheet name).
- DOCX/MD: one section per heading (Heading 1/2/3 in DOCX, #/##/### in MD),
  named after the heading text. Falls back to fixed-size line blocks if the
  file has no headings at all.
- TXT/MD-without-headings/DOCX-without-headings: fixed ~50-line blocks
  ("Bloc 1", "Bloc 2", ...).
- Any section/page longer than ~150 lines (XLSX sheets, DOCX/MD sections)
  is further split into "<name> (partie N)" parts - a PDF/PPTX page is
  exempt, it stays a single unit no matter its length.

OCR (pytesseract + the system tesseract-ocr/tesseract-ocr-fra packages,
see the Dockerfile) is best-effort: a page/image OCR fails silently into
"no text found" rather than raising, so one unreadable scan never breaks
extraction of the rest of a document.
"""

import re
from functools import lru_cache
from dataclasses import dataclass
from io import BytesIO

SUPPORTED_EXTENSIONS = {"txt", "md", "csv", "pdf", "xlsx", "xls", "docx", "pptx", "jpg", "jpeg", "png", "webp"}

BLOCK_SIZE = 50
MAX_SECTION_LINES = 150


class UnsupportedFileTypeError(ValueError):
    pass


class ExtractionError(RuntimeError):
    """`kind` says WHY there is no text, so the app never lumps a photo
    together with a corrupt file:
      "no_text_image" - a valid image, OCR ran, no text in it (a photo, an
                        icon): not an error - visual analysis may describe it;
      "no_text_scan"  - a PDF whose pages have no text layer and no OCR text;
      "ocr_failed"    - the OCR engine itself failed on a valid image;
      "corrupt"       - the file cannot be opened/parsed (damaged, wrong format);
      "empty"         - opened fine but holds no data (empty sheet, blank doc)."""

    def __init__(self, message: str, kind: str = "corrupt"):
        super().__init__(message)
        self.kind = kind


# No text, but nothing wrong with the file either.
NO_TEXT_KINDS = frozenset({"no_text_image", "no_text_scan"})


@dataclass(frozen=True)
class ExtractedPage:
    page_no: int | None
    section: str | None
    line_offset: int
    text: str


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    pages: list[ExtractedPage]


def extract_text(filename: str, content: bytes) -> str:
    """Back-compat wrapper for callers that only need the flattened text."""
    return extract(filename, content).text


def extract(filename: str, content: bytes) -> ExtractionResult:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"Type de fichier « .{ext or '?'} » non supporté. Formats acceptés : "
            f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}."
        )

    try:
        if ext in ("txt", "md"):
            raw_text = content.decode("utf-8", errors="replace")
            blocks = _extract_markdown_blocks(raw_text) if ext == "md" else None
            if blocks is None:
                blocks = _line_blocks(raw_text.splitlines())
        elif ext == "pdf":
            blocks = _extract_pdf_blocks(content)
        elif ext == "xlsx":
            blocks = _extract_xlsx_blocks(content)
        elif ext == "xls":
            blocks = _extract_xls_blocks(content)
        elif ext == "csv":
            blocks = _extract_csv_blocks(content)
        elif ext == "docx":
            blocks = _extract_docx_blocks(content)
        elif ext == "pptx":
            blocks = _extract_pptx_blocks(content)
        elif ext in ("jpg", "jpeg", "png", "webp"):
            blocks = _extract_image_blocks(content)
        else:  # pragma: no cover - unreachable given the set check above
            raise UnsupportedFileTypeError(ext)
    except UnsupportedFileTypeError:
        raise
    except ExtractionError as exc:
        # Raised on purpose by a format extractor: keep its kind.
        raise ExtractionError(f"Impossible de lire le contenu de « {filename} » : {exc}", kind=exc.kind) from exc
    except Exception as exc:  # noqa: BLE001 - surface as a clean 422, not a 500
        raise ExtractionError(
            f"Impossible de lire le contenu de « {filename} » : fichier endommagé ou format inattendu ({exc})",
            kind="corrupt",
        ) from exc

    result = _assemble(blocks)
    if not result.text:
        raise ExtractionError("aucun texte extractible dans ce fichier", kind="empty")
    return result


def _assemble(blocks: list[tuple[int | None, str | None, str]]) -> ExtractionResult:
    """Turns an ordered list of (page_no, section, text) raw blocks into the
    flattened text (identical shape to the pre-page-tracking version: blocks
    joined by a blank line) plus the ExtractedPage list, with each page's
    line_offset computed against that same flattened text."""
    parts: list[str] = []
    pages: list[ExtractedPage] = []
    line_cursor = 1
    for page_no, section, text in blocks:
        # PostgreSQL text columns reject NUL bytes, which some PDFs embed.
        text = text.replace("\x00", "").strip("\n")
        if section:
            section = section.replace("\x00", "")
        if not text.strip():
            continue
        pages.append(ExtractedPage(page_no=page_no, section=section, line_offset=line_cursor, text=text))
        parts.append(text)
        line_cursor += text.count("\n") + 1 + 1  # lines in this block + the blank separator line
    return ExtractionResult(text="\n\n".join(parts), pages=pages)


def _line_blocks(lines: list[str], block_size: int = BLOCK_SIZE) -> list[tuple[None, str, str]]:
    blocks: list[tuple[None, str, str]] = []
    for i in range(0, len(lines), block_size):
        chunk = lines[i : i + block_size]
        text = "\n".join(chunk).strip()
        if text:
            blocks.append((None, f"Bloc {len(blocks) + 1}", text))
    return blocks


def _split_long_section(section: str | None, text: str, max_lines: int = MAX_SECTION_LINES) -> list[tuple[None, str | None, str]]:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return [(None, section, text)]
    parts: list[tuple[None, str | None, str]] = []
    for i in range(0, len(lines), max_lines):
        chunk = "\n".join(lines[i : i + max_lines]).strip()
        if chunk:
            part_no = len(parts) + 1
            name = f"{section} (partie {part_no})" if section else f"partie {part_no}"
            parts.append((None, name, chunk))
    return parts


_HEADING_STYLE_RE = re.compile(r"heading|titre", re.IGNORECASE)
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _heading_sections_to_blocks(sections: list[tuple[str | None, list[str]]]) -> list[tuple[None, str | None, str]] | None:
    """Given [(heading_or_None, [lines...]), ...], returns None if there was
    no real heading at all (caller should fall back to fixed-size blocks),
    otherwise the assembled, length-capped block list."""
    has_heading = any(title is not None for title, _ in sections)
    if not has_heading:
        return None
    blocks: list[tuple[None, str | None, str]] = []
    for title, lines in sections:
        text = "\n".join(lines).strip()
        if not text:
            continue
        name = title or "(avant le premier titre)"
        blocks.extend(_split_long_section(name, text))
    return blocks


def _ocr_image_bytes(image_bytes: bytes) -> str:
    """Best-effort OCR - never raises: a page/shape that can't be OCR'd
    (corrupt image, tesseract missing at runtime, unsupported format)
    contributes empty text rather than failing the whole document."""
    try:
        from PIL import Image
        import pytesseract

        image = Image.open(BytesIO(image_bytes))
        return pytesseract.image_to_string(image, lang="fra+eng").strip()
    except Exception:  # noqa: BLE001 - OCR is a best-effort enhancement, not a hard requirement
        return ""


@lru_cache(maxsize=1)
def _ocr_engine_available() -> bool:
    """Tells "no text in this image" apart from "the OCR engine is broken"
    (an empty OCR result means the former only if the engine runs)."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001
        return False


def _ocr_pdf_page(content: bytes, page_index: int) -> str:
    import fitz  # pymupdf

    pdf = fitz.open(stream=content, filetype="pdf")
    try:
        pixmap = pdf[page_index].get_pixmap(dpi=200)
        return _ocr_image_bytes(pixmap.tobytes("png"))
    finally:
        pdf.close()


def _extract_pdf_blocks(content: bytes) -> list[tuple[int, None, str]]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    blocks: list[tuple[int, None, str]] = []
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if not text:
            # No text layer on this page specifically (scanned/image page) -
            # OCR just this page, never the whole document, so a mixed
            # PDF (some real text, some scans) keeps its real text exact.
            text = _ocr_pdf_page(content, i)
        if text:
            blocks.append((i + 1, None, text))
    if not blocks:
        raise ExtractionError(
            "PDF scanné sans texte extractible, même après OCR (pages blanches, photos ou scan illisible)",
            kind="no_text_scan",
        )
    return blocks


def _extract_xlsx_blocks(content: bytes) -> list[tuple[None, str, str]]:
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(content), data_only=True, read_only=True)
    blocks: list[tuple[None, str, str]] = []
    for sheet in workbook.worksheets:
        lines = []
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                lines.append(" | ".join(cells))
        text = "\n".join(lines).strip()
        if text:
            blocks.extend(_split_long_section(sheet.title, f"Feuille : {sheet.title}\n{text}"))
    if not blocks:
        raise ExtractionError("le classeur ne contient aucune donnée exploitable", kind="empty")
    return blocks


def _extract_xls_blocks(content: bytes) -> list[tuple[None, str, str]]:
    import xlrd

    workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
    blocks: list[tuple[None, str, str]] = []
    for sheet in workbook.sheets():
        lines: list[str] = []
        for row_index in range(sheet.nrows):
            cells = [str(value).strip() for value in sheet.row_values(row_index) if str(value).strip()]
            if cells:
                lines.append(" | ".join(cells))
        text = "\n".join(lines).strip()
        if text:
            blocks.extend(_split_long_section(sheet.name, f"Feuille : {sheet.name}\n{text}"))
    workbook.release_resources()
    if not blocks:
        raise ExtractionError("le classeur ne contient aucune donnée exploitable", kind="empty")
    return blocks


def _extract_csv_blocks(content: bytes) -> list[tuple[None, str, str]]:
    """Same shape as an xlsx sheet: header then one line per row, cells
    joined by " | ". Encoding: UTF-8 (with or without BOM), else Windows-1252
    (Excel's default CSV export on French Windows). Delimiter sniffed among
    ; , tab | - French exports use ';'."""
    import csv
    import io

    # Python's default cell limit (128 KB) rejects real exports whose long
    # "description" cells exceed it; the file size cap already bounds this.
    csv.field_size_limit(max(csv.field_size_limit(), 64 * 1024 * 1024))
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes almost anything
        text = content.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if text[:8192].count(";") >= text[:8192].count(",") else ","
    lines = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        cells = [c.strip() for c in row if c and c.strip()]
        if cells:
            lines.append(" | ".join(cells))
    body = "\n".join(lines).strip()
    if not body:
        raise ExtractionError("le fichier CSV ne contient aucune donnée exploitable", kind="empty")
    return _split_long_section("CSV", body)


def _extract_docx_blocks(content: bytes) -> list[tuple[None, str | None, str]]:
    from docx import Document as DocxDocument

    doc = DocxDocument(BytesIO(content))
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style_name = getattr(paragraph.style, "name", "") or ""
        if _HEADING_STYLE_RE.search(style_name):
            sections.append((text, []))
        else:
            sections[-1][1].append(text)

    table_lines: list[str] = []
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                table_lines.append(" | ".join(cells))
    if table_lines:
        sections.append(("Tableaux", table_lines))

    blocks = _heading_sections_to_blocks(sections)
    if blocks is None:
        all_lines = [line for _, lines in sections for line in lines]
        blocks = list(_line_blocks(all_lines))
    if not blocks:
        raise ExtractionError("le document ne contient aucun texte", kind="empty")
    return blocks


def _extract_markdown_blocks(raw_text: str) -> list[tuple[None, str | None, str]] | None:
    lines = raw_text.splitlines()
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    found_heading = False
    for line in lines:
        match = _MD_HEADING_RE.match(line)
        if match:
            found_heading = True
            sections.append((match.group(2).strip(), []))
        else:
            sections[-1][1].append(line)
    if not found_heading:
        return None
    return _heading_sections_to_blocks(sections)


def _extract_pptx_blocks(content: bytes) -> list[tuple[int, None, str]]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    presentation = Presentation(BytesIO(content))
    blocks: list[tuple[int, None, str]] = []
    for i, slide in enumerate(presentation.slides):
        lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    lines.append(text)
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        lines.append(" | ".join(cells))
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                ocr_text = _ocr_image_bytes(shape.image.blob)
                if ocr_text:
                    lines.append(ocr_text)
        text = "\n".join(lines).strip()
        if text:
            blocks.append((i + 1, None, text))
    if not blocks:
        raise ExtractionError("aucun texte extractible dans cette présentation", kind="empty")
    return blocks


def _extract_image_blocks(content: bytes) -> list[tuple[None, None, str]]:
    """Unlike _ocr_image_bytes (best effort, for PDF pages), tells a damaged
    image, an OCR engine failure and an image with no text apart."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ExtractionError(f"image endommagée ou illisible ({exc})", kind="corrupt") from exc
    text = _ocr_image_bytes(content)
    if not text and not _ocr_engine_available():
        raise ExtractionError("OCR échoué : moteur Tesseract indisponible sur le serveur", kind="ocr_failed")
    if not text:
        raise ExtractionError(
            "aucun texte détecté par OCR (photo, schéma ou illustration) - analyse visuelle possible",
            kind="no_text_image",
        )
    return [(None, None, text)]
