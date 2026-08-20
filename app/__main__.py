"""Запуск: python -m app [--window] [--port 8770]

Обычный запуск поднимает локальный сервер и открывает браузер. С --window тот
же сервер живёт в своём окне (нужен pywebview). Наружу не слушает — только
127.0.0.1, приложение однопользовательское.
"""
from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8770


def run_window(url: str, port: int) -> None:
    import webview                            # noqa: PLC0415

    from app.server import app as server      # noqa: PLC0415

    webview.create_window("Tender-PTS", url, width=1360, height=880)
    threading.Thread(
        target=lambda: uvicorn.run(server, host=HOST, port=port, log_level="warning"),
        daemon=True,
    ).start()
    webview.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Отбор тендеров РБ по профилю ПТС")
    parser.add_argument("--window", action="store_true", help="открыть в своём окне")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://{HOST}:{args.port}/"
    if args.window:
        run_window(url, args.port)
        return

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.server:app", host=HOST, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
