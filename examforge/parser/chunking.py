"""Reparto del temario en bloques con cobertura de principio a fin.

La app nunca pide todas las preguntas sobre el mismo fragmento: el texto se
divide en secciones, las secciones en piezas de tamaño acotado y las preguntas
se reparten proporcionalmente a la extensión de cada pieza. Si hay menos
preguntas que piezas, las piezas se agrupan en bloques contiguos para que todas
las zonas del temario reciban al menos una pregunta.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from examforge.models import QuestionType
from examforge.parser.exclusions import detect_heading

_HEADING_GAP = re.compile(r"\n{2,}")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


@dataclass(slots=True)
class Piece:
    text: str
    sections: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass(slots=True)
class ChunkPlan:
    index: int
    sections: list[str]
    text: str
    question_count: int
    question_types: list[QuestionType]
    chars: int
    truncated: bool = False

    def public_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "sections": self.sections,
            "question_count": self.question_count,
            "question_types": [question_type.value for question_type in self.question_types],
            "chars": self.chars,
            "truncated": self.truncated,
        }


def split_sections(text: str) -> list[tuple[str, str]]:
    """Divide por encabezados y devuelve pares (ruta de sección, contenido)."""
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    title = "Documento"

    def flush() -> None:
        content = "\n".join(buffer).strip()
        if content:
            sections.append((title, content))
        buffer.clear()

    for line in text.splitlines():
        stripped = line.strip()
        heading = detect_heading(stripped) if stripped else None
        if heading:
            flush()
            level, heading_title = heading
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading_title))
            title = " > ".join(part for _, part in stack[-3:])
            buffer.append(stripped)
        else:
            buffer.append(line)
    flush()
    return sections or [("Documento", text.strip())]


def build_pieces(text: str, max_chars: int = 4500) -> list[Piece]:
    """Convierte secciones en piezas de tamaño manejable sin cortar frases."""
    if max_chars < 800:
        raise ValueError("max_chars debe ser al menos 800.")
    pieces: list[Piece] = []
    for section_path, content in split_sections(text):
        paragraphs = [part.strip() for part in _HEADING_GAP.split(content) if part.strip()]
        current: list[str] = []
        current_chars = 0

        def flush_current() -> None:
            nonlocal current_chars
            if current:
                pieces.append(Piece("\n\n".join(current), [section_path]))
                current.clear()
                current_chars = 0

        for paragraph in paragraphs:
            for fragment in _split_paragraph(paragraph, max_chars):
                candidate = current_chars + len(fragment) + (2 if current else 0)
                if current and candidate > max_chars:
                    flush_current()
                current.append(fragment)
                current_chars += len(fragment) + (2 if len(current) > 1 else 0)
        flush_current()

    merged: list[Piece] = []
    minimum = min(500, max(180, max_chars // 8))
    pending: Piece | None = None
    for piece in pieces:
        if pending is not None:
            piece = Piece(
                f"{pending.text}\n\n{piece.text}",
                _unique(pending.sections + piece.sections),
            )
            pending = None
        if piece.chars < minimum:
            if merged:
                previous = merged[-1]
                previous.text = f"{previous.text}\n\n{piece.text}"
                previous.sections = _unique(previous.sections + piece.sections)
            else:
                pending = piece
        else:
            merged.append(piece)
    if pending is not None:
        if merged:
            merged[0] = Piece(
                f"{pending.text}\n\n{merged[0].text}",
                _unique(pending.sections + merged[0].sections),
            )
        else:
            merged.append(pending)
    return merged


def plan_chunks(
    text: str,
    total_questions: int,
    weights: Mapping[QuestionType, int],
    max_chars: int = 4500,
) -> list[ChunkPlan]:
    """Genera bloques ordenados con su cupo exacto de preguntas y tipos."""
    if total_questions < 1:
        raise ValueError("Se necesita al menos una pregunta.")
    pieces = build_pieces(text, max_chars=max_chars)
    if not pieces:
        raise ValueError("El texto no contiene material utilizable.")

    if total_questions >= len(pieces):
        counts = _allocate_by_size([piece.chars for piece in pieces], total_questions)
        groups = [[piece] for piece in pieces]
        question_counts = counts
    else:
        groups = _group_pieces(pieces, total_questions)
        question_counts = [1] * total_questions

    slots = expand_question_types(total_questions, weights)
    plans: list[ChunkPlan] = []
    cursor = 0
    for index, (group, count) in enumerate(zip(groups, question_counts, strict=True), start=1):
        raw_text = "\n\n".join(piece.text for piece in group)
        condensed, truncated = _condense(raw_text, max(max_chars * 3, 14000))
        plans.append(
            ChunkPlan(
                index=index,
                sections=_unique(
                    [section for piece in group for section in piece.sections]
                ),
                text=condensed,
                question_count=count,
                question_types=slots[cursor : cursor + count],
                chars=len(condensed),
                truncated=truncated,
            )
        )
        cursor += count
    return plans


def expand_question_types(
    total: int, weights: Mapping[QuestionType, int]
) -> list[QuestionType]:
    """Reparte los tipos activos con el método del mayor resto y los intercala."""
    positive = {key: max(0, int(value)) for key, value in weights.items() if value > 0}
    if not positive:
        positive = {QuestionType.MULTIPLE_CHOICE: 1}
    active = list(positive)
    if total < len(active):
        active.sort(key=lambda item: positive[item], reverse=True)
        return active[:total]

    raw = {
        question_type: total * positive[question_type] / sum(positive.values())
        for question_type in active
    }
    allocation = {
        question_type: math.floor(raw[question_type]) for question_type in active
    }
    leftovers = total - sum(allocation.values())
    ranked = sorted(
        active,
        key=lambda item: (raw[item] - math.floor(raw[item]), positive[item]),
        reverse=True,
    )
    for question_type in ranked[:leftovers]:
        allocation[question_type] += 1

    # Un formato activo no puede quedar fuera si hay hueco para todos.
    empty = [question_type for question_type in active if allocation[question_type] == 0]
    for question_type in empty:
        donor = max(active, key=lambda item: allocation[item])
        if allocation[donor] <= 1:
            break
        allocation[donor] -= 1
        allocation[question_type] = 1

    sequence: list[QuestionType] = []
    pending = {question_type: allocation[question_type] for question_type in active}
    while len(sequence) < total:
        for question_type in active:
            if pending[question_type] > 0:
                sequence.append(question_type)
                pending[question_type] -= 1
                if len(sequence) == total:
                    break
    return sequence


def _split_paragraph(paragraph: str, limit: int) -> list[str]:
    if len(paragraph) <= limit:
        return [paragraph]
    sentences = [sentence for sentence in _SENTENCE_BOUNDARY.split(paragraph) if sentence]
    fragments: list[str] = []
    current: list[str] = []
    current_chars = 0
    for sentence in sentences:
        if len(sentence) > limit:
            if current:
                fragments.append(" ".join(current))
                current, current_chars = [], 0
            for start in range(0, len(sentence), limit):
                fragments.append(sentence[start : start + limit])
            continue
        if current and current_chars + len(sentence) + 1 > limit:
            fragments.append(" ".join(current))
            current, current_chars = [], 0
        current.append(sentence)
        current_chars += len(sentence) + 1
    if current:
        fragments.append(" ".join(current))
    return fragments


def _allocate_by_size(sizes: Sequence[int], total: int) -> list[int]:
    count = len(sizes)
    if total < count:
        raise ValueError("No se pueden asignar menos preguntas que bloques.")
    allocation = [1] * count
    remaining = total - count
    total_size = sum(sizes) or count
    raw = [remaining * size / total_size for size in sizes]
    for index, amount in enumerate(raw):
        allocation[index] += math.floor(amount)
    leftovers = total - sum(allocation)
    ranked = sorted(
        range(count), key=lambda index: (raw[index] - math.floor(raw[index]), sizes[index]),
        reverse=True,
    )
    for index in ranked[:leftovers]:
        allocation[index] += 1
    return allocation


def _group_pieces(pieces: list[Piece], groups: int) -> list[list[Piece]]:
    total_chars = sum(piece.chars for piece in pieces)
    cumulative: list[int] = []
    running = 0
    for piece in pieces:
        running += piece.chars
        cumulative.append(running)

    boundaries: list[int] = [0]
    for group_index in range(1, groups):
        target = total_chars * group_index / groups
        minimum_index = boundaries[-1] + 1
        maximum_index = len(pieces) - (groups - group_index)
        candidates = range(minimum_index, maximum_index + 1)
        split = min(candidates, key=lambda index: abs(cumulative[index - 1] - target))
        boundaries.append(split)
    boundaries.append(len(pieces))

    return [
        pieces[boundaries[index] : boundaries[index + 1]]
        for index in range(groups)
    ]


def _condense(text: str, limit: int) -> tuple[str, bool]:
    """Muestrea ventanas homogéneas si un bloque excede el contexto prudente."""
    if len(text) <= limit:
        return text, False
    windows = 8
    window_size = max(700, limit // windows)
    stride = (len(text) - window_size) / (windows - 1)
    chunks = [
        text[round(index * stride) : round(index * stride) + window_size].strip()
        for index in range(windows)
    ]
    return "\n\n[ ... ]\n\n".join(chunk for chunk in chunks if chunk), True


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
