"""Lectura tolerante de PDF/Markdown y limpieza del texto resultante."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from examforge.errors import DocumentError, EmptyDocumentError, UnsupportedDocumentError

try:  # PyMuPDF >= 1.24 expone ambos nombres; 2.x recomienda pymupdf.
    import pymupdf
except ImportError:  # pragma: no cover - compatibilidad con instalaciones antiguas
    import fitz as pymupdf  # type: ignore[no-redef]

MAX_FILE_BYTES = 50 * 1024 * 1024
SUPPORTED_SUFFIXES = {".pdf", ".md", ".markdown"}
_WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
_LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\u00ad": "",
    "\u200b": "",
    "\ufeff": "",
}
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


@dataclass(slots=True)
class ExtractedDocument:
    """Resultado de la extracción, todavía sin filtros de contenido."""

    path: str
    name: str
    suffix: str
    text: str
    pages: int | None
    size_bytes: int
    warnings: list[str] = field(default_factory=list)

    @property
    def words(self) -> int:
        return len(_WORD_RE.findall(self.text))

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def tokens(self) -> int:
        """Estimación conservadora para modelos con tokenizador BPE."""
        return max(1, math.ceil(self.chars / 4)) if self.text else 0


def extract_document(path: str | Path) -> ExtractedDocument:
    """Extrae texto plano de un PDF o Markdown; lanza DocumentError si falla."""
    file_path = Path(path).expanduser()
    if not file_path.exists() or not file_path.is_file():
        raise DocumentError(f"No existe el archivo: {file_path.name}")
    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedDocumentError(
            f"Formato no admitido ({suffix or 'sin extensión'}). Usa PDF o Markdown."
        )
    size = file_path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise DocumentError(f"{file_path.name} supera el límite de 50 MB.")

    if suffix == ".pdf":
        text, pages, warnings = _extract_pdf(file_path)
    else:
        text, pages, warnings = _extract_markdown(file_path)

    text = clean_text(text)
    if not text.strip():
        raise EmptyDocumentError(
            f"{file_path.name} no contiene texto seleccionable. "
            "Si es un PDF escaneado, aplica OCR antes de cargarlo."
        )
    return ExtractedDocument(
        path=str(file_path),
        name=file_path.name,
        suffix=suffix,
        text=text,
        pages=pages,
        size_bytes=size,
        warnings=warnings,
    )


def clean_text(raw: str) -> str:
    """Normaliza Unicode, ligaduras, guiones de corte y espaciado."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").translate(_ZERO_WIDTH)
    for original, replacement in _LIGATURES.items():
        text = text.replace(original, replacement)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"(?<=[a-záéíóúüñ])-\s*\n\s*(?=[a-záéíóúüñ])", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _extract_markdown(path: Path) -> tuple[str, None, list[str]]:
    warnings: list[str] = []
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="cp1252", errors="replace")
        warnings.append("El archivo no era UTF-8; se decodificó como Windows-1252.")
    except OSError as exc:
        raise DocumentError(f"No se pudo leer {path.name}: {exc}") from exc

    if raw.startswith("---") :
        frontmatter = re.match(r"^---\s*\n.*?\n---\s*\n", raw, re.DOTALL)
        if frontmatter:
            raw = raw[frontmatter.end() :]
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    raw = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", raw)
    raw = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw)
    raw = re.sub(r"`{3}.*?`{3}", lambda match: match.group(0).strip("`"), raw, flags=re.DOTALL)
    raw = re.sub(r"`([^`\n]+)`", r"\1", raw)
    raw = re.sub(r"(\*\*|__)(.+?)\1", r"\2", raw, flags=re.DOTALL)
    raw = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", raw)
    raw = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"\1", raw)
    raw = re.sub(r"<[^>\n]{1,80}>", "", raw)
    return raw, None, warnings


def _extract_pdf(path: Path) -> tuple[str, int, list[str]]:
    warnings: list[str] = []
    try:
        document = pymupdf.open(path)
    except Exception as exc:  # PyMuPDF lanza RuntimeError/ValueError según el daño
        raise DocumentError(
            f"No se pudo abrir {path.name}. El PDF está dañado o no es válido."
        ) from exc

    try:
        if document.needs_pass:
            raise DocumentError(
                f"{path.name} está protegido con contraseña. "
                "Elimina la protección antes de importarlo."
            )
        if document.page_count == 0:
            raise EmptyDocumentError(f"{path.name} no tiene páginas.")

        pages_text: list[list[str]] = []
        empty_pages = 0
        table_pages = 0
        for page in document:
            lines, has_tables = _page_content(page)
            if not lines:
                empty_pages += 1
            if has_tables:
                table_pages += 1
            pages_text.append(lines)

        if table_pages:
            warnings.append(
                f"Se reconstruyeron tablas en {table_pages} página(s) como filas de texto."
            )

        if empty_pages == document.page_count:
            raise EmptyDocumentError(
                f"{path.name} parece un PDF escaneado sin capa de texto. "
                "Aplica OCR antes de cargarlo."
            )
        if empty_pages:
            warnings.append(
                f"{empty_pages} de {document.page_count} páginas no contienen texto "
                "seleccionable (posibles imágenes)."
            )

        pages_text = _remove_running_headers(pages_text)
        page_blocks = [
            _reflow_paragraphs("\n".join(lines)) for lines in pages_text if lines
        ]
        text = "\n\n".join(block for block in page_blocks if block)
        return text, document.page_count, warnings
    finally:
        document.close()


def _page_content(page: "pymupdf.Page") -> tuple[list[str], bool]:
    """Texto de la página, sustituyendo las tablas por filas Markdown.

    PyMuPDF duplica el contenido de una tabla al extraer bloques y celdas, por
    eso se descartan los bloques cuyo rectángulo se solapa con una tabla.
    """
    tables: list[object] = []
    try:
        finder = page.find_tables()
        tables = list(finder.tables)
    except Exception:  # las tablas son opcionales; nunca deben romper la lectura
        tables = []

    table_rects = [pymupdf.Rect(table.bbox) for table in tables]  # type: ignore[attr-defined]
    lines: list[str] = []
    for block in page.get_text("blocks", sort=True) or []:
        if len(block) >= 7 and block[6] != 0:
            continue
        block_rect = pymupdf.Rect(block[:4])
        if any(block_rect.intersects(rect) for rect in table_rects):
            continue
        lines.extend(
            line.strip()
            for line in str(block[4]).splitlines()
            if line.strip()
        )

    for table in tables:
        try:
            rows = table.extract()  # type: ignore[attr-defined]
        except Exception:
            continue
        rendered = _render_table(rows)
        if rendered:
            lines.extend(["", *rendered, ""])
    return lines, bool(tables)


def _render_table(rows: list[list[str | None]] | None) -> list[str]:
    if not rows:
        return []
    cleaned: list[list[str]] = []
    for row in rows:
        cells = [
            re.sub(r"\s+", " ", str(cell or "")).strip().replace("|", "\\|")[:240]
            for cell in row
        ]
        if any(cells) and cells not in cleaned:
            cleaned.append(cells)
    if len(cleaned) < 2:
        return []
    width = max(len(row) for row in cleaned)
    normalized = [row + [""] * (width - len(row)) for row in cleaned]
    if width == 1:
        return [row[0] for row in normalized if row[0]]
    return ["| " + " | ".join(row) + " |" for row in normalized]


def _remove_running_headers(pages: list[list[str]]) -> list[list[str]]:
    """Elimina cabeceras/pies repetidos (número de página, título del curso)."""
    if len(pages) < 3:
        return pages
    threshold = max(3, math.ceil(len(pages) * 0.6))
    candidates: dict[str, int] = {}
    for lines in pages:
        edge_lines = {line.casefold() for line in lines[:2] + lines[-2:]}
        for line in edge_lines:
            if 2 <= len(line) <= 120:
                candidates[line] = candidates.get(line, 0) + 1
    repeated = {line for line, count in candidates.items() if count >= threshold}
    if not repeated:
        return pages
    return [
        [line for line in lines if line.casefold() not in repeated] for lines in pages
    ]


def _reflow_paragraphs(raw: str) -> str:
    """Recompone líneas cortadas por el maquetador del PDF en párrafos."""
    paragraphs: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            joined = " ".join(part.strip() for part in buffer)
            paragraphs.append(re.sub(r"\s+", " ", joined).strip())
            buffer.clear()

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if re.match(r"^(?:[-*•·]|\d+[.)])\s+", stripped) and buffer:
            flush()
        buffer.append(stripped)
    flush()
    return "\n\n".join(paragraphs)
