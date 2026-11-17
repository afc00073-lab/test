from __future__ import annotations

import json
import re

import pytest

from examforge.ai.demo import generate_demo_exam
from examforge.ai.generator import (
    generate_exam,
    ground_source_quote,
    parse_questions_payload,
)
from examforge.ai.providers import GroqProvider
from examforge.errors import AuthenticationError, RateLimitError, ResponseFormatError
from examforge.models import ExamConfig, QuestionType

MATERIAL = """# Tema 1. La célula

La célula es la unidad estructural y funcional de los seres vivos.
La membrana plasmática regula el intercambio de sustancias con el medio.
El núcleo contiene el material genético y coordina la actividad celular.
Las mitocondrias producen ATP mediante la respiración celular.
Los ribosomas sintetizan proteínas a partir del ARN mensajero.

# Tema 2. El ADN

El ADN almacena la información hereditaria en forma de secuencia de bases.
La replicación del ADN ocurre antes de la división celular.
Las mutaciones pueden alterar la función de una proteína.
"""


class FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        count_match = re.search(r"exactamente (\d+) preguntas", user_prompt)
        count = int(count_match.group(1)) if count_match else 1
        questions = []
        for index in range(count):
            questions.append(
                {
                    "type": "multiple_choice",
                    "prompt": f"¿Qué afirma el material en la cuestión {self.calls}-{index}?",
                    "options": ["La célula", "El ADN", "La membrana", "El núcleo"],
                    "correct_index": 0,
                    "correct_answer": "La célula",
                    "explanation": "El texto define la célula como unidad estructural.",
                    "source_quote": "La célula es la unidad estructural y funcional de los seres vivos.",
                    "topic": "La célula",
                }
            )
        return json.dumps({"questions": questions}, ensure_ascii=False)


def test_parse_questions_accepts_fenced_json_and_reports_garbage() -> None:
    raw = '```json\n{"questions": [{"type": "true_false"}]}\n```'
    assert parse_questions_payload(raw)[0]["type"] == "true_false"
    with pytest.raises(ResponseFormatError):
        parse_questions_payload("Lo siento, no puedo generar eso.")


def test_grounding_requires_a_literal_quote() -> None:
    quote = "La célula es la unidad estructural y funcional de los seres vivos."
    assert ground_source_quote(quote, MATERIAL) == quote
    assert ground_source_quote("Los cloroplastos fabrican glucosa en la oscuridad.", MATERIAL) is None


def test_generate_exam_marks_grounded_questions_and_coverage() -> None:
    provider = FakeProvider()
    config = ExamConfig(
        question_count=2,
        weights={QuestionType.MULTIPLE_CHOICE: 100},
        chunk_chars=12000,
    )
    exam = generate_exam(
        text=MATERIAL,
        document_names=["tema.md"],
        config=config,
        provider=provider,
        model="fake-1",
    )
    assert len(exam.questions) == 2
    assert all(question.source_quote in MATERIAL for question in exam.questions)
    assert all(question.id.startswith("q") for question in exam.questions)
    assert exam.coverage and sum(item.question_count for item in exam.coverage) == 2
    assert provider.calls >= 1


def test_demo_exam_is_valid_and_respects_mix() -> None:
    config = ExamConfig(
        question_count=6,
        weights={
            QuestionType.MULTIPLE_CHOICE: 50,
            QuestionType.TRUE_FALSE: 30,
            QuestionType.SHORT_ANSWER: 20,
        },
        chunk_chars=1200,
    )
    exam = generate_demo_exam(text=MATERIAL, document_names=["tema.md"], config=config)
    assert len(exam.questions) == 6
    assert {question.type for question in exam.questions} == {
        QuestionType.MULTIPLE_CHOICE,
        QuestionType.TRUE_FALSE,
        QuestionType.SHORT_ANSWER,
    }
    assert all(question.source_quote in MATERIAL for question in exam.questions)
    assert exam.provider == "demo"


class _Response:
    def __init__(self, status: int, payload: dict | None = None, headers: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload else ""

    def json(self) -> dict:
        return self._payload


def _groq_ok() -> dict:
    return {
        "choices": [
            {"message": {"content": '{"questions": [{"type": "true_false"}]}'}}
        ]
    }


def test_groq_maps_auth_and_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GroqProvider("clave-invalida", "llama-3.3-70b-versatile")
    monkeypatch.setattr(provider, "_sleep", lambda *args, **kwargs: None)
    provider.session.post = lambda *args, **kwargs: _Response(401)  # type: ignore[method-assign]
    with pytest.raises(AuthenticationError):
        provider.complete_json("sistema", "usuario")

    successful = GroqProvider("clave", "llama-3.3-70b-versatile")
    monkeypatch.setattr(successful, "_sleep", lambda *args, **kwargs: None)
    successful.session.post = lambda *args, **kwargs: _Response(  # type: ignore[method-assign]
        429, headers={"Retry-After": "0"}
    )
    with pytest.raises(RateLimitError):
        successful.complete_json("sistema", "usuario")


def test_groq_retries_one_rate_limit_then_returns_content(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = GroqProvider("clave", "llama-3.3-70b-versatile")
    monkeypatch.setattr(provider, "_sleep", lambda *args, **kwargs: None)
    responses = iter([_Response(429), _Response(200, _groq_ok())])
    provider.session.post = lambda *args, **kwargs: next(responses)  # type: ignore[method-assign]
    content = provider.complete_json("sistema", "usuario")
    assert '"questions"' in content
