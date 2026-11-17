"""Orquestación de la generación: cobertura, proveedor y validación."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Sequence

from pydantic import ValidationError

from examforge.ai.prompts import SYSTEM_PROMPT, build_chunk_prompt, build_repair_prompt
from examforge.ai.providers import ChatProvider
from examforge.errors import GenerationError, ResponseFormatError
from examforge.models import (
    CoverageEntry,
    ExamConfig,
    GeneratedExam,
    Question,
    QuestionType,
    utc_now_iso,
)
from examforge.parser.chunking import ChunkPlan, plan_chunks

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


@dataclass(slots=True)
class ChunkResult:
    questions: list[Question]
    errors: list[str]
    warnings: list[str]


def parse_questions_payload(raw: str) -> list[dict[str, Any]]:
    """Extrae la lista de preguntas de una respuesta JSON, tolerando cercas Markdown."""
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    start_candidates = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not start_candidates:
        raise ResponseFormatError("La respuesta del modelo no contiene JSON.")
    start = min(start_candidates)
    end_object = text.rfind("}")
    end_array = text.rfind("]")
    end = max(end_object, end_array)
    if end <= start:
        raise ResponseFormatError("El JSON del modelo está incompleto.")
    snippet = text[start : end + 1]
    try:
        payload = json.loads(snippet)
    except json.JSONDecodeError as exc:
        raise ResponseFormatError(
            "El modelo devolvió un JSON inválido.", details=exc.msg
        ) from exc
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("questions")
        if items is None:
            items = payload.get("preguntas")
    else:
        items = None
    if not isinstance(items, list) or not items:
        raise ResponseFormatError("El JSON no contiene una lista de preguntas utilizable.")
    return [item for item in items if isinstance(item, dict)]


def _normalize_for_match(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip().casefold()


def ground_source_quote(quote: str, material: str) -> str | None:
    """Devuelve una cita literal verificable del material o None si no la encuentra."""
    cleaned = quote.strip().strip("“”\"'")
    cleaned = re.sub(r"^\.{3}|\.{3}$", "", cleaned).strip()
    if len(cleaned) < 12:
        return None
    haystack = _normalize_for_match(material)
    needle = _normalize_for_match(cleaned)
    if needle in haystack:
        return cleaned[:400]
    parts = [
        part.strip()
        for part in re.split(r"\s*(?:\.{3}|…|\[\.\.\.\])\s*", cleaned)
        if len(part.strip()) >= 20
    ]
    for part in parts:
        if _normalize_for_match(part) in haystack:
            return part[:400]

    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|\n\n", material)
        if len(sentence.strip()) >= 20
    ]
    best: tuple[float, str] | None = None
    for sentence in sentences:
        ratio = SequenceMatcher(None, needle, _normalize_for_match(sentence)).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, sentence)
    if best and best[0] >= 0.82:
        return best[1][:400]
    return None


def _source_document(sections: Sequence[str], document_names: Sequence[str]) -> str:
    joined = " > ".join(sections).casefold()
    for name in document_names:
        stem = Path(name).stem.casefold()
        if stem and stem in joined:
            return name
    return document_names[0] if len(document_names) == 1 else ""


def _coerce_question(
    item: dict[str, Any],
    *,
    chunk: ChunkPlan,
    document_names: Sequence[str],
) -> Question:
    question = Question.model_validate(item)
    if question.type not in chunk.question_types:
        raise ValueError(f"formato no solicitado ({question.type.value})")
    grounded = ground_source_quote(question.source_quote, chunk.text)
    if not grounded:
        raise ValueError("la cita textual no aparece en el material")
    question.source_quote = grounded
    question.source_document = _source_document(chunk.sections, document_names)
    question.topic = question.topic.strip() or " > ".join(chunk.sections[-1:])
    return question


def _generate_chunk(
    provider: ChatProvider,
    *,
    chunk: ChunkPlan,
    total_chunks: int,
    config: ExamConfig,
    document_names: Sequence[str],
) -> ChunkResult:
    expected_counts = Counter(chunk.question_types)
    prompt = build_chunk_prompt(
        chunk=chunk,
        total_chunks=total_chunks,
        difficulty=config.difficulty,
        document_titles=list(document_names),
    )
    errors: list[str] = []
    warnings: list[str] = []
    accepted: list[Question] = []
    seen: set[str] = set()

    for attempt in range(2):
        raw = provider.complete_json(
            SYSTEM_PROMPT,
            prompt
            if attempt == 0
            else build_repair_prompt(
                previous_response=last_raw if "last_raw" in locals() else "",
                errors=errors,
                expected_count=chunk.question_count,
                chunk_text=chunk.text,
            ),
        )
        last_raw = raw
        try:
            payload = parse_questions_payload(raw)
        except ResponseFormatError as exc:
            errors.append(exc.message)
            continue

        for item in payload:
            try:
                question = _coerce_question(item, chunk=chunk, document_names=document_names)
            except (ValidationError, ValueError) as exc:
                errors.append(_short_validation_error(exc))
                continue
            if question.type not in expected_counts:
                continue
            if sum(1 for existing in accepted if existing.type is question.type) >= expected_counts[question.type]:
                continue
            key = re.sub(r"\W+", "", question.prompt.casefold())[:160]
            if key in seen:
                errors.append("pregunta duplicada")
                continue
            seen.add(key)
            accepted.append(question)

        if len(accepted) == chunk.question_count:
            break

    missing = chunk.question_count - len(accepted)
    if missing > 0:
        warnings.append(
            f"El bloque {chunk.index} devolvió {len(accepted)}/{chunk.question_count} preguntas válidas."
        )
    if errors:
        logger.info("Incidencias en bloque %s: %s", chunk.index, "; ".join(errors[:6]))
    return ChunkResult(questions=accepted, errors=errors, warnings=warnings)


def _short_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()))
        return f"{location}: {first.get('msg', 'formato inválido')}"
    return str(exc)[:180]


def build_exam_title(question_count: int, document_names: Sequence[str]) -> str:
    if not document_names:
        return f"Examen de {question_count} preguntas"
    stem = Path(document_names[0]).stem.strip() or "Material"
    return f"Examen de {question_count} preguntas · {stem}"


def generate_exam(
    *,
    text: str,
    document_names: Sequence[str],
    config: ExamConfig,
    provider: ChatProvider,
    model: str,
    progress: ProgressCallback | None = None,
) -> GeneratedExam:
    """Genera el examen completo con cobertura planificada de principio a fin."""
    plans = plan_chunks(
        text,
        total_questions=config.question_count,
        weights=config.weights,
        max_chars=config.chunk_chars,
    )
    total_chunks = len(plans)
    if progress:
        progress(0, total_chunks, f"Temario dividido en {total_chunks} bloques")

    questions: list[Question] = []
    warnings: list[str] = []
    coverage: list[CoverageEntry] = []
    seen_prompts: set[str] = set()

    for chunk in plans:
        if progress:
            progress(
                chunk.index - 1,
                total_chunks,
                f"Generando bloque {chunk.index} de {total_chunks}",
            )
        result = _generate_chunk(
            provider,
            chunk=chunk,
            total_chunks=total_chunks,
            config=config,
            document_names=document_names,
        )
        warnings.extend(result.warnings)
        for question in result.questions:
            key = re.sub(r"\W+", "", question.prompt.casefold())[:160]
            if key in seen_prompts:
                warnings.append("Se descartó una pregunta repetida entre bloques.")
                continue
            seen_prompts.add(key)
            questions.append(question)
        coverage.append(
            CoverageEntry(
                index=chunk.index,
                sections=chunk.sections,
                question_count=chunk.question_count,
                chars=chunk.chars,
                truncated=chunk.truncated,
            )
        )
        if chunk.truncated:
            warnings.append(
                f"El bloque {chunk.index} se muestreó por tamaño; revisa la cobertura."
            )
        if progress:
            progress(
                chunk.index,
                total_chunks,
                f"Bloque {chunk.index} completado",
            )

    if not questions:
        raise GenerationError(
            "No se obtuvo ninguna pregunta válida. Revisa el documento y vuelve a intentarlo."
        )

    if len(questions) > config.question_count:
        questions = questions[: config.question_count]
    if len(questions) < config.question_count:
        warnings.append(
            f"El proveedor solo produjo {len(questions)} preguntas válidas de "
            f"{config.question_count} solicitadas."
        )

    for index, question in enumerate(questions, start=1):
        question.id = f"q{index:03d}"

    title = build_exam_title(len(questions), document_names)
    return GeneratedExam(
        id=f"exam-{utc_now_iso().replace(':', '').replace('-', '')[:15]}",
        title=title,
        config=config,
        questions=questions,
        warnings=list(dict.fromkeys(warnings)),
        coverage=coverage,
        documents=list(document_names),
        provider=provider.name,
        model=model,
    )
