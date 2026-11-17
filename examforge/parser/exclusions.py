"""Filtro de material marcado como fuera del temario.

Las notas al margen y los epígrafes se interpretan con una heurística de
encabezados (Markdown, numeración 1.2.3 o líneas en mayúsculas). Un epígrafe
marcado excluye su sección completa hasta el siguiente encabezado del mismo
nivel o superior; una marca dentro de un párrafo excluye solo ese párrafo.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from examforge.models import DEFAULT_EXCLUSION_KEYWORDS


@dataclass(frozen=True, slots=True)
class ExcludedBlock:
    reason: str
    section: str
    chars: int
    preview: str
    line: int


@dataclass(slots=True)
class SanitizedDocument:
    text: str
    excluded: list[ExcludedBlock] = field(default_factory=list)
    original_chars: int = 0

    @property
    def kept_chars(self) -> int:
        return len(self.text)

    @property
    def excluded_chars(self) -> int:
        return sum(block.chars for block in self.excluded)

    @property
    def exclusion_ratio(self) -> float:
        if not self.original_chars:
            return 0.0
        return self.excluded_chars / self.original_chars


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", stripped).strip()


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_NUMBERED_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})[.)]?\s+(\S.{1,110})$")
_SENTENCE_END = re.compile(r"[.;:,]$")


class ExclusionFilter:
    """Aplica una lista negra configurable sobre el texto extraído."""

    def __init__(self, keywords: Sequence[str] | None = None) -> None:
        raw_keywords = list(keywords) if keywords is not None else list(DEFAULT_EXCLUSION_KEYWORDS)
        normalized: dict[str, str] = {}
        for keyword in raw_keywords:
            clean = keyword.strip()
            if clean:
                normalized.setdefault(_normalize(clean), clean)
        if not normalized:
            normalized = {"no entra": "no entra"}
        self._keywords = normalized
        ordered = sorted(normalized, key=len, reverse=True)
        patterns = [re.escape(keyword).replace(r"\ ", r"\s+") for keyword in ordered]
        self._pattern = re.compile(
            r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)", re.IGNORECASE
        )

    def find_marker(self, text: str) -> str | None:
        normalized = _normalize(text)
        match = self._pattern.search(normalized)
        if not match:
            return None
        return self._keywords.get(normalized[match.start() : match.end()], match.group(0))

    def sanitize(self, text: str) -> SanitizedDocument:
        return sanitize_document(text, self)


def _heading_level(line: str) -> tuple[int, str] | None:
    if len(line) > 140:
        return None
    md_match = _MD_HEADING.match(line)
    if md_match:
        return len(md_match.group(1)), md_match.group(2).strip()

    numbered = _NUMBERED_HEADING.match(line)
    if numbered:
        numbering, title = numbered.groups()
        if len(title.split()) <= 16 and not _SENTENCE_END.search(title):
            return min(4, numbering.count(".") + 1), f"{numbering}. {title.strip()}"

    letters = [char for char in line if char.isalpha()]
    if (
        3 <= len(line) <= 90
        and len(letters) >= 3
        and line == line.upper()
        and not _SENTENCE_END.search(line)
        and not re.match(r"^[\d\W_]+$", line)
    ):
        return 2, line
    return None


def sanitize_document(
    text: str, exclusion_filter: ExclusionFilter | None = None
) -> SanitizedDocument:
    """Devuelve el texto válido y un registro de lo descartado."""
    exclusion_filter = exclusion_filter or ExclusionFilter()
    lines = text.splitlines()
    output: list[str] = []
    excluded: list[ExcludedBlock] = []
    section_stack: list[tuple[int, str]] = []
    paragraph: list[str] = []
    excluded_until: int | None = None
    active: dict[str, object] | None = None
    original_chars = len(text)

    def section_path(extra: str | None = None) -> str:
        titles = [title for _, title in section_stack]
        if extra:
            titles.append(extra)
        return " > ".join(titles[-3:]) or "Documento"

    def close_active() -> None:
        nonlocal active
        if active is not None and int(active["chars"]) > 0:
            excluded.append(
                ExcludedBlock(
                    reason=str(active["reason"]),
                    section=str(active["section"]),
                    chars=int(active["chars"]),
                    preview=str(active["preview"])[:180],
                    line=int(active["line"]),
                )
            )
        active = None

    def flush_paragraph() -> None:
        if not paragraph:
            return
        joined = re.sub(r"\s+", " ", " ".join(paragraph)).strip()
        paragraph.clear()
        if not joined:
            return
        marker = exclusion_filter.find_marker(joined)
        if marker:
            excluded.append(
                ExcludedBlock(
                    reason=marker,
                    section=section_path(),
                    chars=len(joined),
                    preview=joined[:180],
                    line=line_number,
                )
            )
            return
        output.append(joined)
        output.append("")

    line_number = 1
    for raw_line in lines:
        line = raw_line.strip()

        if excluded_until is not None:
            heading = _heading_level(line) if line else None
            if heading and heading[0] <= excluded_until:
                close_active()
                excluded_until = None
            else:
                if active is not None and line:
                    active["chars"] = int(active["chars"]) + len(line) + 1
                line_number += 1
                continue

        if not line:
            flush_paragraph()
            line_number += 1
            continue

        heading = _heading_level(line)
        if heading:
            flush_paragraph()
            level, title = heading
            marker = exclusion_filter.find_marker(title)
            if marker:
                while section_stack and section_stack[-1][0] >= level:
                    section_stack.pop()
                close_active()
                active = {
                    "reason": marker,
                    "section": section_path(title),
                    "chars": len(line),
                    "preview": line,
                    "line": line_number,
                }
                excluded_until = level
                line_number += 1
                continue

            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()
            section_stack.append((level, title))
            output.append(line)
            output.append("")
        else:
            paragraph.append(line)
        line_number += 1

    flush_paragraph()
    close_active()
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()
    return SanitizedDocument(text=cleaned, excluded=excluded, original_chars=original_chars)


def summarize_exclusions(blocks: Iterable[ExcludedBlock], limit: int = 12) -> list[dict[str, str | int]]:
    """Resumen serializable para la vista previa de la interfaz."""
    return [
        {
            "reason": block.reason,
            "section": block.section,
            "chars": block.chars,
            "preview": block.preview,
            "line": block.line,
        }
        for block in list(blocks)[:limit]
    ]
