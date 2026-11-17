"""Generador local de demostración.

Permite probar la interfaz, la cobertura y la exportación sin API key. No
sustituye al motor de IA: construye preguntas heurísticas a partir de frases
literales del material y lo advierte en el examen.
"""

from __future__ import annotations

import hashlib
import random
import re
from pathlib import Path
from typing import Callable, Sequence

from examforge.errors import GenerationError
from examforge.models import (
    CoverageEntry,
    ExamConfig,
    GeneratedExam,
    Question,
    QuestionType,
    utc_now_iso,
)
from examforge.parser.chunking import ChunkPlan, plan_chunks

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")
_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ\-]{4,}")
_STOPWORDS = {
    "sobre", "entre", "desde", "hasta", "donde", "cuando", "porque", "aunque",
    "también", "puede", "pueden", "debe", "deben", "suele", "suelen", "tiene",
    "tienen", "hace", "hacen", "forma", "parte", "través", "mediante", "según",
    "cada", "otro", "otra", "otros", "otras", "este", "esta", "estos", "estas",
    "todo", "toda", "todos", "todas", "mismo", "misma", "dentro", "fuera",
    "mayor", "menor", "primer", "primera", "puede", "ser", "está", "están",
    "como", "para", "pero", "más", "menos", "cuando", "donde", "mientras",
}


def _sentences(text: str) -> list[str]:
    result: list[str] = []
    for fragment in _SENTENCE_SPLIT.split(text):
        sentence = re.sub(r"\s+", " ", fragment).strip(" -–—•\t")
        if sentence.startswith("|") or sentence.startswith("#"):
            continue
        words = sentence.split()
        if 7 <= len(words) <= 45 and 45 <= len(sentence) <= 320:
            result.append(sentence)
    return result


def _terms(text: str) -> list[str]:
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for match in _WORD.finditer(text):
        word = match.group(0).strip("-")
        if len(word) < 5 or word.casefold() in _STOPWORDS:
            continue
        key = word.casefold()
        counts[key] = counts.get(key, 0) + 1
        display.setdefault(key, word)
    ranked = sorted(counts, key=lambda item: (counts[item], len(item)), reverse=True)
    return [display[key] for key in ranked[:80]]


def _key_term(sentence: str, fallback_terms: Sequence[str]) -> str | None:
    candidates = [
        match.group(0)
        for match in _WORD.finditer(sentence)
        if match.group(0).casefold() not in _STOPWORDS
    ]
    if candidates:
        return max(candidates, key=len)
    for term in fallback_terms:
        if term.casefold() in sentence.casefold():
            return term
    return None


def _replace_once(text: str, needle: str, replacement: str) -> str:
    return re.sub(re.escape(needle), replacement, text, count=1, flags=re.IGNORECASE)


def _build_question(
    slot: QuestionType,
    sentence: str,
    terms: Sequence[str],
    rng: random.Random,
) -> Question | None:
    key = _key_term(sentence, terms)
    if not key:
        return None
    alternatives = [term for term in terms if term.casefold() != key.casefold()]
    if len(alternatives) < 3:
        alternatives = list(dict.fromkeys(alternatives + ["Concepto relacionado", "Elemento auxiliar", "Ninguna de las anteriores"]))
    quote = sentence
    if slot is QuestionType.MULTIPLE_CHOICE:
        distractors = rng.sample(alternatives, 3)
        options = [key, *distractors]
        rng.shuffle(options)
        cloze = _replace_once(sentence, key, "________")
        return Question(
            type=slot,
            prompt=f"Completa la afirmación según el material: «{cloze}»",
            options=options,
            correct_index=options.index(key),
            correct_answer=key,
            explanation=f"El material afirma: «{sentence}». El término correcto es «{key}».",
            source_quote=quote,
            topic="",
        )

    if slot is QuestionType.TRUE_FALSE:
        make_false = rng.random() < 0.5
        if make_false:
            replacement = rng.choice(alternatives)
            statement = _replace_once(sentence, key, replacement)
            return Question(
                type=slot,
                prompt=f"¿Verdadero o falso? «{statement}»",
                options=["Verdadero", "Falso"],
                correct_index=1,
                correct_answer="Falso",
                explanation=(
                    f"La afirmación es falsa: el material dice «{sentence}», "
                    f"no «{replacement}»."
                ),
                source_quote=quote,
            )
        return Question(
            type=slot,
            prompt=f"¿Verdadero o falso? «{sentence}»",
            options=["Verdadero", "Falso"],
            correct_index=0,
            correct_answer="Verdadero",
            explanation=f"La afirmación reproduce el contenido literal del material: «{sentence}».",
            source_quote=quote,
        )

    cloze = _replace_once(sentence, key, "________")
    return Question(
        type=QuestionType.SHORT_ANSWER,
        prompt=f"Completa el hueco: «{cloze}»",
        options=[],
        correct_index=None,
        correct_answer=key,
        explanation=f"La respuesta correcta es «{key}», según el material: «{sentence}».",
        source_quote=quote,
    )


def _source_document(sections: Sequence[str], names: Sequence[str]) -> str:
    joined = " > ".join(sections).casefold()
    for name in names:
        stem = Path(name).stem.casefold()
        if stem and stem in joined:
            return name
    return names[0] if len(names) == 1 else ""


def generate_demo_exam(
    *,
    text: str,
    document_names: Sequence[str],
    config: ExamConfig,
    progress: Callable[[int, int, str], None] | None = None,
) -> GeneratedExam:
    plans: list[ChunkPlan] = plan_chunks(
        text,
        total_questions=config.question_count,
        weights=config.weights,
        max_chars=config.chunk_chars,
    )
    seed = hashlib.sha256(text.encode("utf-8")).hexdigest()
    rng = random.Random(seed)
    terms = _terms(text)
    questions: list[Question] = []
    coverage: list[CoverageEntry] = []

    for plan_index, chunk in enumerate(plans, start=1):
        sentences = _sentences(chunk.text)
        if progress:
            progress(plan_index - 1, len(plans), f"Bloque {plan_index} de {len(plans)}")
        if not sentences:
            coverage.append(
                CoverageEntry(
                    index=chunk.index,
                    sections=chunk.sections,
                    question_count=chunk.question_count,
                    chars=chunk.chars,
                    truncated=chunk.truncated,
                )
            )
            continue
        cursor = 0
        for slot in chunk.question_types:
            built: Question | None = None
            for offset in range(len(sentences)):
                candidate = sentences[(cursor + offset) % len(sentences)]
                built = _build_question(slot, candidate, terms, rng)
                if built:
                    cursor = (cursor + offset + 1) % len(sentences)
                    break
            if built:
                built.source_document = _source_document(chunk.sections, document_names)
                built.topic = " > ".join(chunk.sections[-1:])
                questions.append(built)
        coverage.append(
            CoverageEntry(
                index=chunk.index,
                sections=chunk.sections,
                question_count=chunk.question_count,
                chars=chunk.chars,
                truncated=chunk.truncated,
            )
        )
        if progress:
            progress(plan_index, len(plans), f"Bloque {plan_index} completado")

    if not questions:
        raise GenerationError(
            "El material es demasiado corto para construir preguntas. "
            "Añade texto con afirmaciones completas."
        )

    questions = questions[: config.question_count]
    for index, question in enumerate(questions, start=1):
        question.id = f"q{index:03d}"
    stem = Path(document_names[0]).stem if document_names else "material"
    title = f"Examen de {len(questions)} preguntas · {stem}"
    return GeneratedExam(
        id=f"demo-{utc_now_iso().replace(':', '').replace('-', '')[:15]}",
        title=title,
        config=config,
        questions=questions,
        warnings=[
            "Modo demostración: preguntas heurísticas generadas localmente. "
            "Configura Groq o Gemini para obtener evaluación semántica real."
        ]
        + (
            [f"Solo se pudieron construir {len(questions)} de {config.question_count} preguntas."]
            if len(questions) < config.question_count
            else []
        ),
        coverage=coverage,
        documents=list(document_names),
        provider="demo",
        model="heuristic-v1",
    )
