"""Хранилище: SQLite, одна база в work/tenders.db.

Приложение однопользовательское, поэтому ни ORM, ни миграций — схема создаётся
при старте, новые поля добавляются через ALTER TABLE в `_upgrade`.

Важное свойство: закупки и лоты перезаписываются при каждом прогоне (площадка —
источник истины), а решения оператора и прочитанные с чертежей массы живут в
отдельных таблицах и переживают пересбор.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
    reason       TEXT
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
    conn.commit()


# Поля, которых у ГИАС нет: площадки отдают разный набор, а строка в базе одна.
PURCHASE_EXTRAS = {"industry": None, "contacts": None, "etp_url": None}


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
               price, delivery, state, kind, grp, keywords, reason)
           VALUES (:id, :purchase_id, :lot_number, :title, :okpb, :volume, :unit,
               :price, :delivery, :state, :kind, :grp, :keywords, :reason)""",
        lots,
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
    sql = LOT_LIST_SQL
    args: list = []
    if decision == "new":
        sql += " AND d.decision IS NULL"
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
