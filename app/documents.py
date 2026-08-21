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

from app import logs, settings, store

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


HTML_MARKS = (b"<!doctype html", b"<html", b"<!--", b"<head", b"<script")
TITLE_RE = re.compile(rb"(?is)<title[^>]*>(.{0,200}?)</title>")


def page_instead_of_file(content_type: str, head: bytes, name: str) -> str:
    """Не файл, а веб-страница? Тогда объяснить, какая именно.

    Так ведут себя закрытые по региону площадки: на запрос документа приходит
    HTTP 200 и собственная главная страница. Раньше она молча ложилась на диск
    под именем `.pdf`, оператор видел «скачан» — и получал «Не удалось загрузить
    PDF-документ». Проверять надо содержимое, а не код ответа.
    """
    if (name or "").lower().endswith((".htm", ".html")):
        return ""
    low = head[:2048].lower()
    looks_html = content_type.lower().startswith("text/html") or \
        any(mark in low for mark in HTML_MARKS)
    if not looks_html:
        return ""
    found = TITLE_RE.search(head[:4096])
    title = ""
    if found:
        try:
            title = found.group(1).decode("utf-8", "replace").strip()
        except Exception:
            title = ""
    return f"площадка вернула страницу «{title}», а не файл" if title else \
        "площадка вернула страницу, а не файл"


def looks_broken(path: Path) -> bool:
    """Уже лежащий на диске файл — на самом деле сохранённая веб-страница?"""
    try:
        with path.open("rb") as f:
            head = f.read(2048)
    except OSError:
        return True
    return bool(page_instead_of_file("", head, path.name))


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
        if not looks_broken(target):
            _mark(conn, purchase_id, idx, str(target), "готов")
            return {"ok": True, "status": "готов", "size": target.stat().st_size,
                    "local": target.name}
        # Осталось от прежних прогонов: под именем документа лежит HTML-страница.
        target.unlink(missing_ok=True)
        logs.log.warning("файл %s был сохранён как страница — качаю заново",
                         row["name"])

    sess = sess or session()
    # Площадка отдаёт документ тому, кто пришёл с карточки закупки, а не по
    # голой ссылке, поэтому идём как браузер — с Referer и cookie сессии.
    referer = _referer(conn, purchase_id)

    def fetch(verify: bool) -> int | str:
        """Скачать в файл: число — размер, строка — отказ с объяснением."""
        headers = {"Referer": referer} if referer else {}
        with sess.get(row["url"], timeout=TIMEOUT, stream=True, verify=verify,
                      headers=headers) as r:
            if r.status_code != 200:
                return f"площадка ответила {r.status_code}"
            stream = r.iter_content(64 * 1024)
            head = next(stream, b"")
            refusal = page_instead_of_file(
                r.headers.get("Content-Type", ""), head, row["name"])
            if refusal:
                return refusal
            target_dir.mkdir(parents=True, exist_ok=True)
            size = 0
            with target.open("wb") as f:
                for chunk in ([head] if head else []):
                    size += len(chunk)
                    f.write(chunk)
                for chunk in stream:
                    size += len(chunk)
                    if size > MAX_BYTES:
                        f.close()
                        target.unlink(missing_ok=True)
                        return "файл больше 80 МБ, качайте вручную"
                    f.write(chunk)
        return size

    done = "готов"
    try:
        try:
            outcome = fetch(True)
        except requests.exceptions.SSLError:
            # icetrade.by не отдаёт промежуточный сертификат: рукопожатие проходит,
            # а проверка цепочки падает. Документы там публичные, поэтому делается
            # одна попытка без проверки — но оператор видит это в статусе файла.
            outcome = fetch(False)
            done = "готов, TLS без проверки"
        if isinstance(outcome, str):
            _mark(conn, purchase_id, idx, None, outcome)
            return {"ok": False, "status": outcome}
        size = outcome
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

    _mark(conn, purchase_id, idx, str(target), done)
    logs.log.info("файл %s: %s, %s КБ", row["name"], done, round(size / 1024))
    return {"ok": True, "status": done, "size": size, "local": target.name}


def download_purchase(conn, purchase_id: str) -> dict:
    """Скачать всю документацию закупки одним нажатием."""
    rows = store.files_of(conn, purchase_id)
    sess = session()
    logs.log.info("скачиваю документацию закупки %s: файлов %s",
                  purchase_id, len(rows))
    # Сначала карточка — ради cookie сессии: по голой ссылке площадка отдаёт
    # документ не всегда, а пришедшему со страницы закупки — отдаёт.
    page = _referer(conn, purchase_id)
    if page:
        try:
            sess.get(page, timeout=30, verify=False)
        except Exception:
            pass                       # не открылась — попробуем файлы как есть
    done, failed, status = 0, 0, ""
    for row in rows:
        res = download_one(conn, purchase_id, row["idx"], sess)
        if res["ok"]:
            done += 1
        else:
            failed += 1
            status = res["status"]
            logs.log.warning("файл %s не скачан: %s", row["name"], status)
    conn.commit()
    logs.log.info("закупка %s: скачано %s из %s", purchase_id, done, len(rows))
    return {"downloaded": done, "failed": failed, "total": len(rows),
            "status": status}


def local_path(conn, purchase_id: str, idx: int) -> Path | None:
    row = conn.execute(
        "SELECT local FROM files WHERE purchase_id = ? AND idx = ?",
        (purchase_id, idx)).fetchone()
    if not row or not row["local"]:
        return None
    path = Path(row["local"])
    if not path.is_file():
        return None
    if looks_broken(path):
        # Открывать нечего: под именем документа лежит страница площадки.
        _mark(conn, purchase_id, idx, None, "площадка вернула страницу, а не файл")
        logs.log.warning("файл %s оказался страницей, а не документом", path.name)
        return None
    return path


def _referer(conn, purchase_id: str) -> str:
    row = conn.execute("SELECT page_url, auction_url FROM purchases WHERE id = ?",
                       (purchase_id,)).fetchone()
    if not row:
        return ""
    return row["page_url"] or row["auction_url"] or ""


def _mark(conn, purchase_id: str, idx: int, local: str | None, status: str) -> None:
    conn.execute("UPDATE files SET local = ?, status = ? WHERE purchase_id = ? AND idx = ?",
                 (local, status, purchase_id, idx))
    conn.commit()
