"""Скачивание тендерной документации.

Отдельно от источника, потому что файлы лежат не там, где данные. ГИАС отдаёт
метаданные и ссылки без ограничений, а сами файлы — на goszakupki.by, и туда
доступ бывает закрыт по региону.

Отсюда и смысл серверного скачивания вместо простой ссылки: браузер оператора
ходит напрямую и настройки прокси приложения не знает, а этот код ходит через
тот прокси, который задан в настройках. Поэтому «скачать» работает там, где
«открыть» упирается в блокировку.
"""
from __future__ import annotations

import re
from pathlib import Path

import requests

from app import settings, store

DOCS = settings.ROOT / "work" / "docs"
MAX_BYTES = 80 * 1024 * 1024        # 80 МБ: тендерные архивы бывают крупными
TIMEOUT = 120

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "*/*",
}


def safe_name(name: str, idx: int) -> str:
    """Имя файла для диска. Транслит площадки сохраняем — он несёт номенклатуру."""
    cleaned = re.sub(r"[^A-Za-zА-Яа-яЁё0-9._-]+", "_", name or "").strip("._")
    if not cleaned:
        cleaned = "file"
    return f"{idx:02d}_{cleaned[:120]}"


def session(cfg: dict | None = None) -> requests.Session:
    cfg = cfg or settings.read()
    s = requests.Session()
    s.headers.update(HEADERS)
    proxy = (cfg.get("proxy") or "").strip()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def download_one(conn, purchase_id: str, idx: int, sess=None) -> dict:
    """Скачать один файл. Возвращает состояние записи, не бросает наружу сеть."""
    row = conn.execute(
        "SELECT * FROM files WHERE purchase_id = ? AND idx = ?",
        (purchase_id, idx)).fetchone()
    if not row:
        return {"ok": False, "status": "нет такого файла"}

    target_dir = DOCS / purchase_id
    target = target_dir / safe_name(row["name"], idx)
    if target.exists() and target.stat().st_size:
        _mark(conn, purchase_id, idx, str(target), "готов")
        return {"ok": True, "status": "готов", "size": target.stat().st_size,
                "local": target.name}

    sess = sess or session()
    try:
        with sess.get(row["url"], timeout=TIMEOUT, stream=True) as r:
            if r.status_code != 200:
                status = f"площадка ответила {r.status_code}"
                _mark(conn, purchase_id, idx, None, status)
                return {"ok": False, "status": status}
            target_dir.mkdir(parents=True, exist_ok=True)
            size = 0
            with target.open("wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        f.close()
                        target.unlink(missing_ok=True)
                        status = "файл больше 80 МБ, качайте вручную"
                        _mark(conn, purchase_id, idx, None, status)
                        return {"ok": False, "status": status}
                    f.write(chunk)
    except requests.exceptions.ProxyError as e:
        status = f"прокси не отвечает: {str(e)[:80]}"
        _mark(conn, purchase_id, idx, None, status)
        return {"ok": False, "status": status}
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError):
        # Самый частый случай: goszakupki.by закрыт для этого адреса.
        status = "площадка недоступна — нужен прокси"
        _mark(conn, purchase_id, idx, None, status)
        return {"ok": False, "status": status}
    except Exception as e:
        status = f"{type(e).__name__}: {str(e)[:80]}"
        _mark(conn, purchase_id, idx, None, status)
        return {"ok": False, "status": status}

    _mark(conn, purchase_id, idx, str(target), "готов")
    return {"ok": True, "status": "готов", "size": size, "local": target.name}


def download_purchase(conn, purchase_id: str) -> dict:
    """Скачать всю документацию закупки одним нажатием."""
    rows = store.files_of(conn, purchase_id)
    sess = session()
    done, failed, status = 0, 0, ""
    for row in rows:
        res = download_one(conn, purchase_id, row["idx"], sess)
        if res["ok"]:
            done += 1
        else:
            failed += 1
            status = res["status"]
    conn.commit()
    return {"downloaded": done, "failed": failed, "total": len(rows),
            "status": status}


def local_path(conn, purchase_id: str, idx: int) -> Path | None:
    row = conn.execute(
        "SELECT local FROM files WHERE purchase_id = ? AND idx = ?",
        (purchase_id, idx)).fetchone()
    if not row or not row["local"]:
        return None
    path = Path(row["local"])
    return path if path.is_file() else None


def _mark(conn, purchase_id: str, idx: int, local: str | None, status: str) -> None:
    conn.execute("UPDATE files SET local = ?, status = ? WHERE purchase_id = ? AND idx = ?",
                 (local, status, purchase_id, idx))
    conn.commit()
