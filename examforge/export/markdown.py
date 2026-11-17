"""Exportación a Markdown de exámenes generados."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from examforge.models import GeneratedExam, Question, QuestionType

_MONTHS = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def safe_filename(exam: GeneratedExam, include_answers: bool, suffix: str) -> str:
    base = re.sub(r"[^\w\s.-]", "", exam.title, flags=re.UNICODE)
    base = re.sub(r"\s+", "-", base).strip("-.").lower() or "examen"
    base = base[:70]
    return f"{base}{'-soluciones' if include_answers else ''}.{suffix}"


def render_markdown(exam: GeneratedExam, include_answers: bool) -> str:
    lines: list[str] = [f"# {exam.title}", ""]
    lines.append(f"**Documentos:** {', '.join(exam.documents) or 'Material de estudio'}  ")
    lines.append(f"**Generado:** {_format_date(exam.created_at)} · **Preguntas:** {len(exam.questions)}  ")
    lines.append(
        f"**Dificultad:** {exam.config.difficulty.label} · "
        f"**Formatos:** {_format_weights(exam)}  "
    )
    lines.append(f"**Motor:** {exam.provider}{(' · ' + exam.model) if exam.model else ''}")
    lines.extend(["", "---", "", "## Preguntas", ""])

    for index, question in enumerate(exam.questions, start=1):
        lines.append(f"### {index}. {question.type.label}")
        lines.append("")
        lines.append(question.prompt)
        lines.append("")
        if question.type is QuestionType.SHORT_ANSWER:
            lines.append("Respuesta: ______________________________________________")
        else:
            for option_index, option in enumerate(question.options):
                lines.append(f"- **{chr(65 + option_index)})** {option}")
        lines.append("")

    if include_answers:
        lines.extend(["---", "", "## Soluciones", ""])
        for index, question in enumerate(exam.questions, start=1):
            lines.append(f"### {index}. {_answer_text(question)}")
            lines.append("")
            lines.append(question.explanation)
            lines.append("")
            source = f" — *{question.source_document}*" if question.source_document else ""
            lines.append(f"> «{question.source_quote}»{source}")
            lines.append("")

    if any("demostración" in warning.casefold() for warning in exam.warnings):
        lines.extend(
            [
                "---",
                "",
                "> Aviso: este examen se generó en modo demostración con reglas locales. "
                "No ha sido validado por un modelo de lenguaje.",
                "",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def write_markdown(exam: GeneratedExam, path: str | Path, include_answers: bool) -> Path:
    target = Path(path)
    target.write_text(render_markdown(exam, include_answers), encoding="utf-8")
    return target


def _answer_text(question: Question) -> str:
    if question.type is QuestionType.SHORT_ANSWER:
        return f"Respuesta: {question.correct_answer}"
    if question.correct_index is not None and question.correct_index < len(question.options):
        return f"Respuesta: {chr(65 + question.correct_index)}) {question.options[question.correct_index]}"
    return f"Respuesta: {question.correct_answer}"


def _format_weights(exam: GeneratedExam) -> str:
    labels = {
        QuestionType.MULTIPLE_CHOICE: "test",
        QuestionType.TRUE_FALSE: "V/F",
        QuestionType.SHORT_ANSWER: "corta",
    }
    parts = [
        f"{labels[question_type]} {amount} %"
        for question_type, amount in exam.config.weights.items()
        if amount > 0
    ]
    return ", ".join(parts) or "test"


def _format_date(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return value
    return f"{moment.day} {_MONTHS[moment.month - 1]} {moment.year}, {moment:%H:%M}"
