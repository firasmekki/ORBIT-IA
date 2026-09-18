"""Extracts plain text from uploaded office files so it can flow through
the same chunking/embedding pipeline as hand-typed documents (app/rag/ingest.py).

The extracted text - not the original file - is what gets embedded and
searched. The original bytes are kept in MinIO (app/core/storage.py) purely
for download; they are never re-parsed at query time.
"""

from io import BytesIO

SUPPORTED_EXTENSIONS = {"txt", "md", "pdf", "xlsx", "docx"}


class UnsupportedFileTypeError(ValueError):
    pass


class ExtractionError(RuntimeError):
    pass


def extract_text(filename: str, content: bytes) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"Type de fichier « .{ext or '?'} » non supporté. Formats acceptés : "
            f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}."
        )

    try:
        if ext in ("txt", "md"):
            return content.decode("utf-8", errors="replace")
        if ext == "pdf":
            return _extract_pdf(content)
        if ext == "xlsx":
            return _extract_xlsx(content)
        if ext == "docx":
            return _extract_docx(content)
    except UnsupportedFileTypeError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as a clean 422, not a 500
        raise ExtractionError(f"Impossible de lire le contenu de « {filename} » : {exc}") from exc

    raise UnsupportedFileTypeError(ext)  # pragma: no cover - unreachable given the set check above


def _extract_pdf(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text:
        raise ExtractionError("aucun texte extractible (PDF scanné/image sans OCR ?)")
    return text


def _extract_xlsx(content: bytes) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(content), data_only=True, read_only=True)
    blocks: list[str] = []
    for sheet in workbook.worksheets:
        lines = [f"Feuille : {sheet.title}"]
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                lines.append(" | ".join(cells))
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    text = "\n\n".join(blocks)
    if not text:
        raise ExtractionError("le classeur ne contient aucune donnée exploitable")
    return text


def _extract_docx(content: bytes) -> str:
    from docx import Document as DocxDocument

    doc = DocxDocument(BytesIO(content))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                paragraphs.append(" | ".join(cells))
    text = "\n\n".join(paragraphs)
    if not text:
        raise ExtractionError("le document ne contient aucun texte")
    return text
