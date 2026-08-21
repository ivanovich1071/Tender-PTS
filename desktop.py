"""Точка входа собранного приложения: своё окно, без консоли и браузера.

Отдельный файл нужен ровно затем, что у собранного .exe нет ни командной
строки, ни привычки открывать браузер. Всё остальное — тот же сервер на
127.0.0.1, что и при запуске из редактора.

Рядом с .exe появляются `work/` (база, документация, журналы), `.env` (ключ) и
`profile.json` (номенклатура) — их правят руками, поэтому они лежат снаружи, а
не внутри сборки.
"""
from __future__ import annotations

import socket
import threading

import uvicorn

HOST = "127.0.0.1"
PORT = 8770


def free_port(preferred: int = PORT) -> int:
    """Занятый порт — обычное дело: приложение уже запущено из редактора."""
    with socket.socket() as probe:
        try:
            probe.bind((HOST, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def main() -> None:
    import webview

    from app.server import app as server

    port = free_port()
    threading.Thread(
        target=lambda: uvicorn.run(server, host=HOST, port=port,
                                   log_level="warning"),
        daemon=True,
    ).start()
    webview.create_window("Tender-PTS", f"http://{HOST}:{port}/",
                          width=1360, height=880)
    webview.start()


if __name__ == "__main__":
    main()
