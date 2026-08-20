"""HTTP-слой. Только 127.0.0.1 — приложение однопользовательское.

Наружу ничего не слушает и авторизации не имеет намеренно: это локальный
инструмент оператора, а не сервис.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app import (calendar_by, documents, jobs, logs, profile as profile_mod,
                 settings, store)

WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Tender-PTS", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _open_log() -> None:
    logs.setup()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.get("/static/{name}")
def static(name: str) -> FileResponse:
    path = (WEB / name).resolve()
    if not path.is_file() or WEB.resolve() not in path.parents:
        return JSONResponse({"error": "нет такого файла"}, status_code=404)
    return FileResponse(path)


@app.get("/api/state")
def state() -> dict:
    conn = store.connect()
    try:
        counts = dict(conn.execute("""
            SELECT
              (SELECT COUNT(*) FROM purchases) AS purchases,
              (SELECT COUNT(*) FROM lots WHERE kind='supply') AS lots,
              (SELECT COUNT(*) FROM lots l LEFT JOIN decisions d ON d.lot_id=l.id
                WHERE l.kind='supply' AND d.decision IS NULL) AS new
        """).fetchone())
        last = store.last_run(conn)
    finally:
        conn.close()
    prof = profile_mod.load()
    return {
        "job": jobs.state(),
        "counts": counts,
        "last_run": last,
        "log": str(logs.path() or ""),
        "profile": {
            "version": prof.raw.get("version"),
            "groups": len(prof.groups),
            "keywords": len(prof.keywords),
            "organizers": [o["query"] for o in prof.organizers],
        },
    }


@app.post("/api/collect")
def collect_start() -> dict:
    return jobs.start()


@app.post("/api/collect/cancel")
def collect_cancel() -> dict:
    return jobs.cancel()


@app.get("/api/lots")
def lot_list(decision: str = "") -> list[dict]:
    conn = store.connect()
    try:
        rows = store.lots(conn, decision)
    finally:
        conn.close()
    cfg = settings.read()
    for row in rows:
        row["deadline"] = _fmt(row.get("deadline_ms"))
        row["days_left"] = calendar_by.days_left(row.get("deadline_ms"), cfg)
        row["price_per_unit"] = _per_unit(row)
    return rows


@app.get("/api/lots/{lot_id}")
def lot_get(lot_id: str) -> dict:
    conn = store.connect()
    try:
        row = store.lot(conn, lot_id)
        if not row:
            return JSONResponse({"error": "лот не найден"}, status_code=404)
        row["files"] = store.files_of(conn, row["purchase_id"])
        siblings = [dict(r) for r in conn.execute(
            "SELECT id, lot_number, title, price, volume, unit FROM lots "
            "WHERE purchase_id = ? ORDER BY lot_number", (row["purchase_id"],))]
    finally:
        conn.close()
    cfg = settings.read()
    row["deadline"] = _fmt(row.get("deadline_ms"))
    row["days_left"] = calendar_by.days_left(row.get("deadline_ms"), cfg)
    row["price_per_unit"] = _per_unit(row)
    row["siblings"] = siblings
    return row


@app.post("/api/lots/{lot_id}/decision")
async def lot_decision(lot_id: str, request: Request) -> dict:
    body = await request.json()
    conn = store.connect()
    try:
        store.set_decision(conn, lot_id, str(body.get("decision", "")),
                           str(body.get("note", "")))
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/files/{purchase_id}/{idx}/download")
def file_download(purchase_id: str, idx: int) -> dict:
    conn = store.connect()
    try:
        return documents.download_one(conn, purchase_id, idx)
    finally:
        conn.close()


@app.post("/api/purchases/{purchase_id}/download")
def purchase_download(purchase_id: str) -> dict:
    conn = store.connect()
    try:
        return documents.download_purchase(conn, purchase_id)
    finally:
        conn.close()


@app.get("/api/files/{purchase_id}/{idx}")
def file_open(purchase_id: str, idx: int):
    """Отдать скачанный файл. Открывается в приложении, а не на площадке."""
    conn = store.connect()
    try:
        path = documents.local_path(conn, purchase_id, idx)
    finally:
        conn.close()
    if not path:
        return JSONResponse({"error": "файл ещё не скачан"}, status_code=404)
    return FileResponse(path, filename=path.name)


# Четыре хоста, из которых состоит вся работа. Два закрыты по региону, и понять,
# «изнутри» ли запущено приложение, иначе никак.
HOSTS = [
    ("gias.by", "https://gias.by/", "ГИАС — данные госзакупок"),
    ("goszakupki.by", "https://goszakupki.by/", "файлы документации"),
    ("icetrade.by", "https://icetrade.by/robots.txt",
     "закупки за собственные средства"),
    ("zakupki.butb.by", "https://zakupki.butb.by/auctions/reestrauctions.html", "БУТБ"),
]


@app.post("/api/diagnostics")
def diagnostics() -> dict:
    """Достучаться до всех площадок и записать результат в журнал.

    Нужна тому, кто запускает приложение не там, где его писали: по одному
    экрану видно, доступен ли рынок целиком или только его бюджетная часть.
    """
    import requests

    cfg = settings.read()
    proxy = (cfg.get("proxy") or "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"}

    logs.log.info("--- проверка площадок, прокси: %s ---", logs.mask_proxy(proxy))
    checks = []
    for host, url, why in HOSTS:
        detail = ""
        try:
            # verify=False: icetrade не отдаёт промежуточный сертификат, а здесь
            # важно, отвечает ли хост вообще, а не качество его цепочки.
            r = requests.get(url, timeout=20, headers=headers, proxies=proxies,
                             verify=False)
            ok = r.status_code == 200
            if ok:
                note = f"ответил, страница {len(r.content)} байт"
            elif r.status_code == 403:
                note = "403 — адрес не из Беларуси"
            else:
                note = f"ответил {r.status_code}"
        except Exception as e:
            # Читать будет тот, кто запускает, а не тот, кто писал: подробности
            # urllib3 ему ничего не скажут, поэтому они уходят в журнал.
            ok = False
            timeout = isinstance(e, (requests.exceptions.ConnectTimeout,
                                     requests.exceptions.ReadTimeout))
            note = ("не отвечает — похоже, закрыт по региону" if timeout
                    else "не удалось соединиться")
            detail = f"{type(e).__name__}: {str(e)[:120]}"
        checks.append({"host": host, "why": why, "ok": ok, "note": note})
        logs.log.info("  %-18s %s %s", host, "ок " if ok else "нет",
                      f"{note} [{detail}]" if detail else note)

    closed = [c["host"] for c in checks if not c["ok"]]
    verdict = ("все площадки отвечают — виден весь рынок" if not closed
               else "не отвечают: " + ", ".join(closed))
    logs.log.info("вывод: %s", verdict)
    return {"checks": checks, "verdict": verdict,
            "proxy": logs.mask_proxy(proxy), "log": str(logs.path() or "")}


@app.get("/api/settings")
def settings_get() -> dict:
    return settings.read()


@app.put("/api/settings")
async def settings_put(request: Request) -> dict:
    return settings.write(await request.json())


def _fmt(ms) -> str:
    dt = calendar_by.to_minsk(ms)
    return dt.strftime("%d.%m.%Y %H:%M") if dt else ""


def _per_unit(row: dict) -> float | None:
    """Цена за штуку: в ГИАС price — сумма лота, а не цена единицы."""
    price, volume = row.get("price"), row.get("volume")
    if not price or not volume:
        return None
    return round(float(price) / float(volume), 2)
