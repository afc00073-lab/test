from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from examforge.api import ExamForgeApi
from examforge.config import ConfigStore
from examforge.export import write_markdown, write_pdf

MATERIAL = """# Tema 1. La célula

La célula es la unidad estructural y funcional de los seres vivos.
La membrana plasmática regula el intercambio de sustancias.
El núcleo contiene el material genético.
Las mitocondrias producen ATP mediante la respiración celular.
Los ribosomas sintetizan proteínas a partir del ARN mensajero.

## Anexo (no entra en examen)

Este anexo contiene material que no debe evaluarse.

# Tema 2. Genética

El ADN almacena la información hereditaria en forma de secuencia de bases.
La replicación ocurre antes de la división celular.
Las mutaciones pueden alterar la función de una proteína.
"""


@pytest.fixture()
def api(tmp_path: Path) -> ExamForgeApi:
    store = ConfigStore(tmp_path / "config")
    return ExamForgeApi(store)


def _write_source(tmp_path: Path) -> Path:
    source = tmp_path / "tema.md"
    source.write_text(MATERIAL, encoding="utf-8")
    return source


def test_bridge_state_stays_private(api: ExamForgeApi) -> None:
    # PyWebView serializes every public attribute of the js_api object and
    # follows nested objects, so session state must never be public.
    public = {name for name in vars(api) if not name.startswith("_")}
    assert public == set()


def test_document_ingestion_and_coverage(api: ExamForgeApi, tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    response = api.add_documents([str(source)])
    assert response["ok"] is True
    documents = response["data"]["documents"]
    assert len(documents) == 1
    summary = documents[0]
    assert summary["excluded_count"] >= 1
    assert "anexo contiene material" not in summary["preview"].casefold()

    coverage = api.preview_coverage(
        {"question_count": 6, "weights": {"multiple_choice": 60, "true_false": 40}}
    )
    assert coverage["ok"] is True
    chunks = coverage["data"]["chunks"]
    assert chunks
    assert sum(chunk["question_count"] for chunk in chunks) == 6


def test_demo_generation_and_exports(api: ExamForgeApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _write_source(tmp_path)
    assert api.add_documents([str(source)])["ok"]
    saved = api.save_settings({"provider": "demo"})
    assert saved["ok"]

    response = api.generate(
        {
            "question_count": 5,
            "weights": {
                "multiple_choice": 40,
                "true_false": 40,
                "short_answer": 20,
            },
            "difficulty": "university",
            "chunk_chars": 1200,
        }
    )
    assert response["ok"] is True, response
    exam = response["data"]["exam"]
    assert len(exam["questions"]) == 5
    assert "anexo" not in " ".join(question["source_quote"] for question in exam["questions"]).casefold()

    markdown_path = tmp_path / "examen.md"
    monkeypatch.setattr(api, "_ask_save_path", lambda *args, **kwargs: str(markdown_path))
    exported = api.export_exam({"exam": exam, "format": "md", "include_answers": True})
    assert exported["ok"] is True, exported
    text = markdown_path.read_text(encoding="utf-8")
    assert "## Preguntas" in text
    assert "## Soluciones" in text

    pdf_path = tmp_path / "examen.pdf"
    monkeypatch.setattr(api, "_ask_save_path", lambda *args, **kwargs: str(pdf_path))
    exported_pdf = api.export_exam({"exam": exam, "format": "pdf", "include_answers": False})
    assert exported_pdf["ok"] is True, exported_pdf
    assert pdf_path.stat().st_size > 1000


def test_settings_hide_api_keys(api: ExamForgeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    response = api.save_settings(
        {"api_keys": {"groq": "gsk_test_123"}, "provider": "groq"}
    )
    assert response["ok"] is True, response
    settings = response["data"]["settings"]
    assert settings["groq_key_set"] is True
    assert "gsk_test_123" not in str(settings)

    bootstrap = api.bootstrap()
    assert bootstrap["data"]["settings"]["gemini_key_set"] is False
    assert "gsk_test_123" not in str(bootstrap)


def test_exporters_without_answers_do_not_reveal_solutions(tmp_path: Path) -> None:
    from examforge.ai.demo import generate_demo_exam
    from examforge.models import ExamConfig, QuestionType

    exam = generate_demo_exam(
        text=MATERIAL,
        document_names=["tema.md"],
        config=ExamConfig(
            question_count=3,
            weights={QuestionType.MULTIPLE_CHOICE: 100},
            chunk_chars=1200,
        ),
    )
    markdown = tmp_path / "sin-soluciones.md"
    write_markdown(exam, markdown, include_answers=False)
    content = markdown.read_text(encoding="utf-8")
    assert "## Soluciones" not in content
    assert "source_quote" not in content

    pdf_path = tmp_path / "con-soluciones.pdf"
    write_pdf(exam, pdf_path, include_answers=True)
    document = pymupdf.open(pdf_path)
    text = "".join(page.get_text() for page in document)
    document.close()
    assert "Soluciones" in text
    assert exam.questions[0].source_quote[:25] in text
