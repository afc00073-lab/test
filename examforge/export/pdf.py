"""Exportación a PDF limpio usando las fuentes base de PyMuPDF."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # type: ignore[no-redef]

from examforge.models import GeneratedExam, Question, QuestionType

_PAGE = pymupdf.paper_rect("a4")
_MARGIN_X = 54.0
_MARGIN_TOP = 56.0
_MARGIN_BOTTOM = 58.0
_TEXT = (0.09, 0.09, 0.11)
_SECONDARY = (0.32, 0.32, 0.36)
_MUTED = (0.47, 0.47, 0.51)
_ACCENT = (0.18, 0.37, 0.82)
_RULE = (0.85, 0.85, 0.87)


class _PdfBuilder:
    def __init__(self) -> None:
        self.document = pymupdf.open()
        self.page = self._new_page()
        self.y = _MARGIN_TOP

    def _new_page(self):
        page = self.document.new_page(width=_PAGE.width, height=_PAGE.height)
        self.y = _MARGIN_TOP
        return page

    @property
    def _content_width(self) -> float:
        return _PAGE.width - 2 * _MARGIN_X

    def _ensure(self, height: float) -> None:
        if self.y + height > _PAGE.height - _MARGIN_BOTTOM:
            self.page = self._new_page()

    def paragraph(
        self,
        text: str,
        *,
        font: str = "helv",
        size: float = 9.5,
        color: tuple[float, float, float] = _TEXT,
        indent: float = 0.0,
        leading: float = 1.42,
        space_before: float = 0.0,
        space_after: float = 5.0,
    ) -> None:
        self.y += space_before
        line_height = size * leading
        width = self._content_width - indent
        clean = " ".join(text.replace("\r", "").split("\n"))
        for line in _wrap_text(clean, font, size, width):
            self._ensure(line_height)
            self.page.insert_text(
                (_MARGIN_X + indent, self.y + size),
                line,
                fontname=font,
                fontsize=size,
                color=color,
            )
            self.y += line_height
        self.y += space_after

    def heading(self, text: str, *, size: float = 13.5, space_before: float = 12.0) -> None:
        self.paragraph(
            text,
            font="hebo",
            size=size,
            color=_TEXT,
            space_before=space_before,
            space_after=7.0,
        )

    def rule(self, space_before: float = 3.0, space_after: float = 8.0) -> None:
        self.y += space_before
        self._ensure(8)
        self.page.draw_line(
            (_MARGIN_X, self.y),
            (_PAGE.width - _MARGIN_X, self.y),
            color=_RULE,
            width=0.7,
        )
        self.y += space_after

    def finish(self, footer: str) -> None:
        total = self.document.page_count
        for index, page in enumerate(self.document, start=1):
            page.draw_line(
                (_MARGIN_X, _PAGE.height - 38),
                (_PAGE.width - _MARGIN_X, _PAGE.height - 38),
                color=_RULE,
                width=0.7,
            )
            page.insert_text(
                (_MARGIN_X, _PAGE.height - 26),
                footer,
                fontname="helv",
                fontsize=7.5,
                color=_MUTED,
            )
            label = f"{index} / {total}"
            text_width = pymupdf.get_text_length(label, fontname="helv", fontsize=7.5)
            page.insert_text(
                (_PAGE.width - _MARGIN_X - text_width, _PAGE.height - 26),
                label,
                fontname="helv",
                fontsize=7.5,
                color=_MUTED,
            )


def _wrap_text(text: str, font: str, size: float, width: float) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        if pymupdf.get_text_length(candidate, fontname=font, fontsize=size) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _answer_text(question: Question) -> str:
    if question.type is QuestionType.SHORT_ANSWER:
        return question.correct_answer
    if question.correct_index is not None and question.correct_index < len(question.options):
        return question.options[question.correct_index]
    return question.correct_answer


def write_pdf(exam: GeneratedExam, path: str | Path, include_answers: bool) -> Path:
    builder = _PdfBuilder()
    builder.paragraph(exam.title, font="hebo", size=19, leading=1.25, space_before=4, space_after=7)
    meta = (
        f"{len(exam.questions)} preguntas · {exam.config.difficulty.label} · "
        f"{exam.provider}{(' / ' + exam.model) if exam.model else ''}"
    )
    builder.paragraph(meta, size=8.5, color=_MUTED, space_after=2)
    documents = ", ".join(exam.documents) or "Material de estudio"
    builder.paragraph("Documentos: " + documents, size=8.5, color=_MUTED, space_after=9)
    builder.rule(space_after=10)

    builder.heading("Preguntas", size=14, space_before=2)
    for index, question in enumerate(exam.questions, start=1):
        builder.paragraph(
            f"{index}. {question.prompt}",
            font="hebo",
            size=10.2,
            leading=1.45,
            space_before=7,
            space_after=4,
        )
        if question.type is QuestionType.SHORT_ANSWER:
            builder.paragraph(
                "Respuesta: __________________________________________________",
                size=9.2,
                color=_SECONDARY,
                space_after=4,
            )
        else:
            for option_index, option in enumerate(question.options):
                builder.paragraph(
                    f"{chr(65 + option_index)})  {option}",
                    size=9.2,
                    color=_SECONDARY,
                    indent=12,
                    leading=1.35,
                    space_after=2,
                )
        builder.rule(space_before=3, space_after=5)

    if include_answers:
        builder.heading("Soluciones", size=14, space_before=14)
        for index, question in enumerate(exam.questions, start=1):
            builder.paragraph(
                f"{index}. {_answer_text(question)}",
                font="hebo",
                size=9.8,
                color=_ACCENT,
                space_before=6,
                space_after=3,
            )
            builder.paragraph(question.explanation, size=9.1, color=_TEXT, space_after=2)
            citation = f"«{question.source_quote}»"
            if question.source_document:
                citation += f" — {question.source_document}"
            builder.paragraph(citation, size=8.4, color=_MUTED, indent=10, leading=1.35, space_after=4)

    builder.finish("ExamForge · material de estudio")
    target = Path(path)
    builder.document.save(target, garbage=3, deflate=True)
    builder.document.close()
    return target
