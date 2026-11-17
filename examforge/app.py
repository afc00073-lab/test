"""Arranque de la ventana PyWebView y puente de arrastrar y soltar."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import webview

from examforge import __app_name__, __version__
from examforge.api import ExamForgeApi
from examforge.config import ConfigStore, logs_dir

logger = logging.getLogger(__name__)


def resource_path(*parts: str) -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    base = Path(frozen_root) if frozen_root else Path(__file__).resolve().parent
    if frozen_root:
        return base.joinpath("examforge", "ui", *parts)
    return base.joinpath("ui", *parts)


def configure_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    try:
        handler = logging.handlers.RotatingFileHandler(
            logs_dir() / "examforge.log",
            maxBytes=1_500_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except OSError:
        pass
    if debug or not getattr(sys, "frozen", False):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)


def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - equipos antiguos sin shcore
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass


def _window_background(store: ConfigStore) -> str:
    theme = store.load_settings().theme
    if theme == "dark":
        return "#101012"
    if theme == "light":
        return "#f6f6f7"
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return "#f6f6f7" if value else "#101012"
    except OSError:
        return "#f6f6f7"


def _bind_drop_zone(window: webview.Window) -> None:
    """Registra la zona de arrastre en el DOM de PyWebView.

    Los navegadores ocultan la ruta real de un archivo soltado; PyWebView la
    expone en el evento Python y desde ahí se entrega a la interfaz.
    """

    def on_drop(event: dict[str, Any]) -> None:
        try:
            transfer = event.get("dataTransfer") or event.get("domTransfer") or {}
            files = transfer.get("files") or []
            paths = [
                item.get("pywebviewFullPath")
                for item in files
                if isinstance(item, dict) and item.get("pywebviewFullPath")
            ]
            if paths:
                payload = json.dumps(paths, ensure_ascii=False)
                window.evaluate_js(
                    f"window.ExamForge && window.ExamForge.handleDroppedFiles({payload})"
                )
        except Exception:
            logger.exception("No se pudieron procesar los archivos arrastrados")

    try:
        element = window.dom.get_element("#dropzone")
        if element is not None:
            element.on("drop", on_drop)
    except Exception:  # pragma: no cover - depende del motor web
        logger.warning("El motor web no permite enlazar arrastrar y soltar.", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ExamForge", description=__doc__)
    parser.add_argument("--debug", action="store_true", help="Abre DevTools y logs detallados")
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    args = parser.parse_args(argv)

    _enable_dpi_awareness()
    configure_logging(args.debug)
    logger.info("Iniciando %s %s", __app_name__, __version__)

    index = resource_path("index.html")
    if not index.exists():
        _fatal("No se encontró la interfaz empaquetada. Reinstala la aplicación.")
        return 1

    store = ConfigStore()
    api = ExamForgeApi(store)
    window = webview.create_window(
        __app_name__,
        url=str(index),
        js_api=api,
        width=1280,
        height=830,
        min_size=(1024, 700),
        background_color=_window_background(store),
        text_select=True,
        confirm_close=False,
        easy_drag=False,
    )
    api.window = window

    try:
        window.events.loaded += lambda: _bind_drop_zone(window)
    except Exception:  # pragma: no cover
        logger.warning("No se pudo suscribir el evento loaded", exc_info=True)

    try:
        webview.start(debug=args.debug)
    except Exception as exc:  # pragma: no cover - depende de WebView2
        logger.exception("No se pudo iniciar la ventana")
        _fatal(
            "No se pudo iniciar la interfaz. Comprueba que Microsoft Edge WebView2 "
            "Runtime está instalado.\n\nDetalle: " + str(exc)
        )
        return 1
    finally:
        logger.info("Aplicación cerrada")
    return 0


def _fatal(message: str) -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, f"{__app_name__}", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
