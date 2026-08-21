"""Хранилище: SQLite, одна база в work/tenders.db.

Приложение однопользовательское, поэтому ни ORM, ни миграций — схема создаётся
при старте, новые поля добавляются через ALTER TABLE в `_upgrade`.

Важное свойство: закупки и лоты перезаписываются при каждом прогоне (площадка —
источник истины), а решения оператора и прочитанные с чертежей массы живут в
отдельных таблицах и переживают пересбор.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from app import settings

DB = settings.ROOT / "work" / "tenders.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    number       TEXT,
    title        TEXT,
    state        TEXT,
    tender_form  TEXT,
    organizer    TEXT,
    unp          TEXT,
    location     TEXT,
    sum_lot      REAL,
    created_ms   INTEGER,
    updated_ms   INTEGER,
    deadline_ms  INTEGER,
    auction_url  TEXT,
    page_url     TEXT,
    days_left    INTEGER,
    industry     TEXT,
    contacts     TEXT,
    etp_url      TEXT,
    duplicate_of TEXT,
    first_seen   TEXT,
    last_seen    TEXT
);

CREATE TABLE IF NOT EXISTS lots (
    id           TEXT PRIMARY KEY,
    purchase_id  TEXT NOT NULL,
    lot_number   INTEGER,
    title        TEXT,
    okpb         TEXT,
    volume       REAL,
    unit         TEXT,
    price        REAL,
    delivery     TEXT,
    state        TEXT,
    kind         TEXT,
    grp          TEXT,
    keywords     TEXT,
    reason       TEXT,
    verdict      TEXT,
    verdict_why  TEXT,
    verdict_by   TEXT
);
CREATE INDEX IF NOT EXISTS lots_purchase ON lots(purchase_id);

CREATE TABLE IF NOT EXISTS files (
    purchase_id  TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    name         TEXT,
    url          TEXT,
    local        TEXT,
    status       TEXT,
    PRIMARY KEY (purchase_id, idx)
);

-- Переживает пересбор: решения оператора.
CREATE TABLE IF NOT EXISTS decisions (
    lot_id    TEXT PRIMARY KEY,
    decision  TEXT,
    note      TEXT,
    at        TEXT
);

-- Переживает пересбор: вердикт «профиль или мимо» по номенклатуре лота.
-- Ключ — не id лота (площадка выдаёт новый при каждом переразмещении), а хэш
-- от заказчика и названия. Поэтому повторяющаяся из года в год деталь второй
-- раз не стоит ни запроса к модели, а поправка оператора живёт вечно.
CREATE TABLE IF NOT EXISTS verdicts (
    key       TEXT PRIMARY KEY,
    verdict   TEXT,         -- fit | off | maybe
    why       TEXT,
    who       TEXT,         -- модель | оператор
    model     TEXT,
    title     TEXT,         -- ради примеров для модели: ключ — хэш, читать нечего
    organizer TEXT,
    at        TEXT
);

-- Переживает пересбор: то, что оператор выбросил руками. Сбор такие лоты
-- больше не приносит — иначе чистка списка обнулялась бы каждым прогоном.
CREATE TABLE IF NOT EXISTS dismissed (
    key       TEXT PRIMARY KEY,
    organizer TEXT,
    title     TEXT,
    at        TEXT
);

-- Переживает пересбор: то, что прочитано с чертежей.
CREATE TABLE IF NOT EXISTS drawings (
    lot_id      TEXT PRIMARY KEY,
    mass_kg     REAL,
    material    TEXT,
    designation TEXT,
    title       TEXT,
    source_file TEXT,
    confidence  TEXT,
    at          TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started   TEXT,
    finished  TEXT,
    stats     TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _upgrade(conn)
    return conn


def _upgrade(conn: sqlite3.Connection) -> None:
    """Дописать колонки, появившиеся после создания базы.

    CREATE TABLE IF NOT EXISTS не трогает уже существующую таблицу, поэтому у
    того, кто собирал тендеры вчера, новых полей не будет — их добавляем сами.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(purchases)")}
    for column in ("industry", "contacts", "etp_url", "duplicate_of"):
        if column not in have:
            conn.execute(f"ALTER TABLE purchases ADD COLUMN {column} TEXT")
    have = {row["name"] for row in conn.execute("PRAGMA table_info(lots)")}
    for column in ("verdict", "verdict_why", "verdict_by"):
        if column not in have:
            conn.execute(f"ALTER TABLE lots ADD COLUMN {column} TEXT")
    have = {row["name"] for row in conn.execute("PRAGMA table_info(verdicts)")}
    for column in ("title", "organizer"):
        if column not in have:
            conn.execute(f"ALTER TABLE verdicts ADD COLUMN {column} TEXT")
    conn.commit()


def lot_key(organizer: str | None, title: str | None, okpb: str | None = "") -> str:
    """Устойчивый ключ лота: заказчик + название + ОКПБ.

    Идентификатор лота на площадке меняется при каждом переразмещении, а
    номенклатура — нет. Поэтому и вердикт модели, и правка оператора, и отметка
    «выброшено» вешаются на содержание, а не на номер строки.
    """
    from app.profile import norm
    raw = f"{norm(organizer)}|{norm(title)}|{(okpb or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# Поля, которых у ГИАС нет: площадки отдают разный набор, а строка в базе одна.
PURCHASE_EXTRAS = {"industry": None, "contacts": None, "etp_url": None}

# Вердикт проставляется после сбора, поэтому при вставке его может не быть.
LOT_EXTRAS = {"verdict": None, "verdict_why": None, "verdict_by": None}


def save_purchase(conn: sqlite3.Connection, p: dict, lots: list[dict],
                  files: list[dict]) -> None:
    stamp = now()
    conn.execute(
        """INSERT INTO purchases (id, source, number, title, state, tender_form,
               organizer, unp, location, sum_lot, created_ms, updated_ms,
               deadline_ms, auction_url, page_url, days_left, industry, contacts,
               etp_url, first_seen, last_seen)
           VALUES (:id, :source, :number, :title, :state, :tender_form, :organizer,
               :unp, :location, :sum_lot, :created_ms, :updated_ms, :deadline_ms,
               :auction_url, :page_url, :days_left, :industry, :contacts,
               :etp_url, :stamp, :stamp)
           ON CONFLICT(id) DO UPDATE SET
               state=excluded.state, sum_lot=excluded.sum_lot,
               updated_ms=excluded.updated_ms, deadline_ms=excluded.deadline_ms,
               days_left=excluded.days_left, contacts=excluded.contacts,
               last_seen=excluded.last_seen""",
        {**PURCHASE_EXTRAS, **p, "stamp": stamp},
    )
    conn.execute("DELETE FROM lots WHERE purchase_id = ?", (p["id"],))
    conn.executemany(
        """INSERT INTO lots (id, purchase_id, lot_number, title, okpb, volume, unit,
               price, delivery, state, kind, grp, keywords, reason,
               verdict, verdict_why, verdict_by)
           VALUES (:id, :purchase_id, :lot_number, :title, :okpb, :volume, :unit,
               :price, :delivery, :state, :kind, :grp, :keywords, :reason,
               :verdict, :verdict_why, :verdict_by)""",
        [{**LOT_EXTRAS, **lot} for lot in lots],
    )
    if files:
        conn.executemany(
            """INSERT INTO files (purchase_id, idx, name, url, local, status)
               VALUES (:purchase_id, :idx, :name, :url, :local, :status)
               ON CONFLICT(purchase_id, idx) DO UPDATE SET
                   name=excluded.name, url=excluded.url""",
            files,
        )


def mark_duplicates(conn: sqlite3.Connection) -> int:
    """Пометить закупки, которые пришли с двух площадок сразу.

    Госзакупка может лежать и в ГИАС, и на icetrade. Совпадением считается
    один заказчик (УНП), одна и та же минута окончания подачи и одна сумма —
    признак нарочно узкий: лучше пропустить дубль, чем склеить две разные
    закупки одного завода. Дубль не удаляется: состав приложенных файлов у
    площадок разный, и оператору важно видеть обе карточки.
    """
    conn.execute("UPDATE purchases SET duplicate_of = NULL")
    rows = conn.execute(
        """SELECT id, source, unp, deadline_ms, sum_lot, first_seen FROM purchases
            WHERE unp IS NOT NULL AND deadline_ms IS NOT NULL AND sum_lot IS NOT NULL
            ORDER BY first_seen, id""").fetchall()
    primary: dict[tuple, str] = {}
    marked = 0
    for row in rows:
        key = (row["unp"], row["deadline_ms"], round(row["sum_lot"], 2))
        if key in primary and primary[key] != row["id"]:
            conn.execute("UPDATE purchases SET duplicate_of = ? WHERE id = ?",
                         (primary[key], row["id"]))
            marked += 1
        else:
            primary[key] = row["id"]
    conn.commit()
    return marked


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs (started) VALUES (?)", (now(),))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, stats: dict) -> None:
    conn.execute("UPDATE runs SET finished = ?, stats = ? WHERE id = ?",
                 (now(), json.dumps(stats, ensure_ascii=False), run_id))
    conn.commit()


def last_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    data = dict(row)
    if data.get("stats"):
        try:
            data["stats"] = json.loads(data["stats"])
        except ValueError:
            pass
    return data


LOT_LIST_SQL = """
SELECT l.*, p.number, p.title AS purchase_title, p.organizer, p.unp, p.state AS purchase_state,
       p.tender_form, p.deadline_ms, p.days_left, p.auction_url, p.page_url, p.location,
       p.source, p.industry, p.contacts, p.etp_url, p.duplicate_of,
       d.decision, d.note,
       dr.mass_kg, dr.material, dr.designation,
       (SELECT COUNT(*) FROM files f WHERE f.purchase_id = p.id) AS files_count
  FROM lots l
  JOIN purchases p ON p.id = l.purchase_id
  LEFT JOIN decisions d ON d.lot_id = l.id
  LEFT JOIN drawings  dr ON dr.lot_id = l.id
 WHERE l.kind = 'supply'
"""


def lots(conn: sqlite3.Connection, decision: str = "") -> list[dict]:
    """Список лотов для вкладки.

    «Мимо профиля» — не решение оператора, а вердикт по номенклатуре, поэтому
    вкладка отдельная: из работы такие лоты уходят, но остаются видимыми и
    возвращаются одним нажатием.
    """
    sql = LOT_LIST_SQL
    args: list = []
    if decision == "new":
        sql += " AND d.decision IS NULL AND (l.verdict IS NULL OR l.verdict <> 'off')"
    elif decision == "off":
        sql += " AND l.verdict = 'off'"
    elif decision:
        sql += " AND d.decision = ?"
        args.append(decision)
    sql += " ORDER BY p.deadline_ms ASC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def lot(conn: sqlite3.Connection, lot_id: str) -> dict | None:
    row = conn.execute(LOT_LIST_SQL + " AND l.id = ?", (lot_id,)).fetchone()
    return dict(row) if row else None


def files_of(conn: sqlite3.Connection, purchase_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM files WHERE purchase_id = ? ORDER BY idx", (purchase_id,))]


def set_decision(conn: sqlite3.Connection, lot_id: str, decision: str,
                 note: str = "") -> None:
    conn.execute(
        """INSERT INTO decisions (lot_id, decision, note, at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(lot_id) DO UPDATE SET
               decision=excluded.decision, note=excluded.note, at=excluded.at""",
        (lot_id, decision, note, now()))
    conn.commit()


# --- вердикты по номенклатуре и ручная чистка ----------------------------

def verdict_cache(conn: sqlite3.Connection) -> dict[str, dict]:
    """Все известные вердикты разом: их сотни, а не миллионы."""
    return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM verdicts")}


def save_verdict(conn: sqlite3.Connection, key: str, verdict: str, why: str,
                 who: str, model: str = "", title: str = "",
                 organizer: str = "") -> None:
    """Записать вердикт. Решение оператора модель не перебивает."""
    if who != "оператор":
        row = conn.execute("SELECT who FROM verdicts WHERE key = ?", (key,)).fetchone()
        if row and row["who"] == "оператор":
            return
    conn.execute(
        """INSERT INTO verdicts (key, verdict, why, who, model, title, organizer, at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
               verdict=excluded.verdict, why=excluded.why, who=excluded.who,
               model=excluded.model, title=excluded.title,
               organizer=excluded.organizer, at=excluded.at""",
        (key, verdict, why, who, model, title, organizer, now()))


def apply_verdict(conn: sqlite3.Connection, lot_id: str, verdict: str, why: str,
                  who: str) -> None:
    conn.execute(
        "UPDATE lots SET verdict = ?, verdict_why = ?, verdict_by = ? WHERE id = ?",
        (verdict, why, who, lot_id))


def dismissed_keys(conn: sqlite3.Connection) -> set[str]:
    return {r["key"] for r in conn.execute("SELECT key FROM dismissed")}


def dismiss(conn: sqlite3.Connection, key: str, organizer: str, title: str) -> None:
    conn.execute(
        """INSERT INTO dismissed (key, organizer, title, at) VALUES (?, ?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET at=excluded.at""",
        (key, organizer, title, now()))


def undismiss(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM dismissed WHERE key = ?", (key,))


def examples(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    """Прошлые решения оператора — примеры для модели.

    Берётся то, что человек подтвердил руками: «участвуем» и «пропущен» из
    карточек, вердикты, поправленные оператором, и выброшенное из списка. На
    этом модель учится отличать поддон печи от поддона деревянного лучше, чем
    на любых придуманных правилах.
    """
    rows = conn.execute(
        """SELECT l.title, p.organizer,
                  CASE WHEN d.decision = 'participate' THEN 'fit' ELSE 'off' END AS verdict
             FROM decisions d
             JOIN lots l ON l.id = d.lot_id
             JOIN purchases p ON p.id = l.purchase_id
            WHERE d.decision IN ('participate', 'skip')
            ORDER BY d.at DESC LIMIT ?""", (limit,)).fetchall()
    out = [dict(r) for r in rows]
    out += [{"title": r["title"], "organizer": r["organizer"],
             "verdict": r["verdict"]}
            for r in conn.execute(
                """SELECT title, organizer, verdict FROM verdicts
                    WHERE who = 'оператор' AND title <> ''
                    ORDER BY at DESC LIMIT ?""", (limit,))]
    out += [{"title": r["title"], "organizer": r["organizer"], "verdict": "off"}
            for r in conn.execute(
                "SELECT title, organizer FROM dismissed ORDER BY at DESC LIMIT ?",
                (limit,))]
    seen, unique = set(), []
    for item in out:
        low = (item["title"] or "").strip().lower()
        if low and low not in seen:
            seen.add(low)
            unique.append(item)
    return unique[:limit]
