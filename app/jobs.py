"""Фоновый прогон сбора.

Прогон в приложении ровно один: это однопользовательский инструмент, и два
параллельных сбора только били бы по площадке лишними запросами. Поэтому
никакой очереди — состояние одного текущего прогона и возможность его отменить.
"""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field

from app import collector, store


@dataclass
class Run:
    status: str = "idle"          # idle | running | done | error | cancelled
    message: str = ""
    stats: dict = field(default_factory=dict)
    error: str = ""

    def view(self) -> dict:
        return {"status": self.status, "message": self.message,
                "stats": self.stats, "error": self.error}


_current = Run()
_cancel = threading.Event()
_lock = threading.Lock()


def state() -> dict:
    return _current.view()


def busy() -> bool:
    return _current.status == "running"


def cancel() -> dict:
    _cancel.set()
    _current.message = "отменяю…"
    return state()


def start() -> dict:
    global _current
    with _lock:
        if busy():
            return state()
        _cancel.clear()
        _current = Run(status="running", message="запуск")
    threading.Thread(target=_run, daemon=True).start()
    return state()


def _run() -> None:
    conn = store.connect()
    run_id = store.start_run(conn)
    conn.close()
    try:
        stats = collector.collect(
            progress=lambda m: setattr(_current, "message", m),
            cancelled=_cancel.is_set,
        )
        _current.stats = stats
        _current.status = "cancelled" if _cancel.is_set() else "done"
        _current.message = "отменено" if _cancel.is_set() else "готово"
    except Exception as e:
        _current.status = "error"
        _current.error = f"{type(e).__name__}: {e}"
        _current.message = "ошибка"
        traceback.print_exc()
    finally:
        conn = store.connect()
        store.finish_run(conn, run_id, _current.stats or {"error": _current.error})
        conn.close()
