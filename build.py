"""Compila ExamForge como un único .exe sin consola.

Uso:
    python build.py                 # tests + build limpio
    python build.py --skip-tests    # solo empaquetado
    python build.py --no-clean      # reutiliza la caché de PyInstaller
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC = ROOT / "examforge.spec"
ARTIFACT = DIST / "ExamForge.exe"
MIN_PYTHON = (3, 11)


def ensure_python() -> None:
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(
            f"ExamForge necesita Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} o superior. "
            f"Versión actual: {sys.version.split()[0]}"
        )


def ensure_dependencies() -> None:
    missing: list[str] = []
    for module in ("webview", "pymupdf", "requests", "pydantic", "PyInstaller"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        raise SystemExit(
            "Faltan dependencias: " + ", ".join(missing) + "\n"
            "Instala el entorno de desarrollo con:\n"
            "    python -m pip install -r requirements-dev.txt"
        )


def run_tests() -> None:
    print("\n[1/3] Ejecutando tests")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise SystemExit("Los tests han fallado; se cancela el empaquetado.")


def clean() -> None:
    print("\n[2/3] Limpiando artefactos anteriores")
    for directory in (BUILD, DIST):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)


def build() -> None:
    print("\n[3/3] Empaquetando con PyInstaller")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--noconfirm",
    ]
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit("PyInstaller terminó con error.")
    print(f"Empaquetado en {time.monotonic() - started:.1f} s")


def report() -> None:
    if not ARTIFACT.exists():
        raise SystemExit(f"No se encontró el ejecutable esperado: {ARTIFACT}")
    digest = hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()
    size_mb = ARTIFACT.stat().st_size / (1024 * 1024)
    print("\nArtefacto listo")
    print(f"  Ruta:   {ARTIFACT}")
    print(f"  Tamaño: {size_mb:.1f} MB")
    print(f"  SHA256: {digest}")
    print("\nEjecuta el binario directamente; no requiere Python instalado.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compila ExamForge para Windows")
    parser.add_argument("--skip-tests", action="store_true", help="Omite la suite de tests")
    parser.add_argument("--no-clean", action="store_true", help="No borra build/ y dist/")
    args = parser.parse_args(argv)

    ensure_python()
    ensure_dependencies()
    if not args.skip_tests:
        run_tests()
    if not args.no_clean:
        clean()
    build()
    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
