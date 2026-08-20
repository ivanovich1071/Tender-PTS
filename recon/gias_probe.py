#!/usr/bin/env python3
"""Разведка ГИАС (Беларусь) по профилю ООО «Промтехснаб».

Этап 0 плана: задача не построить парсер, а получить цифру — сколько закупок,
соответствующих профилю компании, появляется в неделю.

Источник: внутренний JSON API SPA gias.by, авторизация не требуется.
    POST /search/api/v1/search/purchases
         {"page":0,"pageSize":10,"contextTextSearch":"...",
          "sortField":"dtCreate","sortOrder":"DESC"}
    GET  /purchase/api/v1/purchase/{uuid}

Контракт неофициальный и может измениться без предупреждения — при расхождении
схемы скрипт падает громко, а не тихо возвращает пустоту.

Запуск:
    python recon/gias_probe.py --days 90
    python recon/gias_probe.py --days 180 --proxy socks5://user:pass@host:port
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://gias.by"
SEARCH = f"{BASE}/search/api/v1/search/purchases"
CARD = f"{BASE}/purchase/api/v1/purchase/{{uuid}}"
CARD_URL = f"{BASE}/#/purchase/current/{{uuid}}"

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-cache",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}

REQUIRED_FIELDS = {"publicPurchaseNumber", "title", "dtCreate", "purchaseGiasId"}


def ms_to_dt(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


class Gias:
    def __init__(self, proxy: str | None = None, pause: float = 0.4, timeout: int = 30):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.pause = pause
        self.timeout = timeout
        self.calls = 0

    def _post(self, payload: dict, attempts: int = 4) -> dict:
        last = None
        for i in range(attempts):
            try:
                r = self.s.post(SEARCH, json=payload, timeout=self.timeout)
                self.calls += 1
                if r.status_code == 200:
                    time.sleep(self.pause)
                    return r.json()
                last = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as e:  # сеть, таймаут, обрыв прокси
                last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (i + 1))
        raise RuntimeError(f"ГИАС не ответил после {attempts} попыток. {last}")

    def card(self, uuid: str, attempts: int = 3) -> dict | None:
        for i in range(attempts):
            try:
                r = self.s.get(CARD.format(uuid=uuid), timeout=self.timeout)
                self.calls += 1
                if r.status_code == 200:
                    time.sleep(self.pause)
                    return r.json()
                if r.status_code == 404:
                    return None
            except Exception:
                pass
            time.sleep(1.0 * (i + 1))
        return None

    def search(self, text: str, page: int, page_size: int) -> dict:
        return self._post(
            {
                "page": page,
                "pageSize": page_size,
                "contextTextSearch": text,
                "sortField": "dtCreate",
                "sortOrder": "DESC",
            }
        )


def norm(text: str | None) -> str:
    return re.sub(r"[^а-яёa-z0-9]+", " ", (text or "").lower())


def kw_tokens(keyword: str) -> list[str]:
    return [t for t in norm(keyword).split() if t]


def kw_matches(text_norm: str, tokens: list[str]) -> bool:
    """Грубая морфология: ищем основы, отбрасывая 2 последних символа у длинных слов.

    Точного стемминга здесь не нужно — задача этапа посчитать порядок величины,
    а не построить продакшн-матчинг.
    """
    for t in tokens:
        stem = t[:-2] if len(t) > 5 else t
        if stem not in text_norm:
            return False
    return True


def collect(gias: Gias, keyword: str, group: str, cutoff: datetime,
            page_size: int, max_pages: int, log) -> list[dict]:
    """Страницы идут dtCreate DESC — как только ушли за cutoff, дальше не листаем."""
    rows: list[dict] = []
    page = 0
    total = None
    while page < max_pages:
        data = gias.search(keyword, page, page_size)
        if total is None:
            total = data.get("totalElements", 0)
            if total == 0:
                log(f"    {keyword!r}: 0 за всю историю")
                return rows
        content = data.get("content") or []
        if not content:
            break

        if page == 0:
            missing = REQUIRED_FIELDS - set(content[0].keys())
            if missing:
                raise RuntimeError(
                    f"Схема ответа ГИАС изменилась, нет полей: {sorted(missing)}"
                )

        stop = False
        for it in content:
            created = ms_to_dt(it.get("dtCreate"))
            if created and created < cutoff:
                stop = True
                break
            org = it.get("organizator") or {}
            lot = it.get("sumLot") or {}
            rows.append(
                {
                    "group": group,
                    "keyword": keyword,
                    "purchase_id": it.get("purchaseGiasId"),
                    "number": it.get("publicPurchaseNumber"),
                    "title": (it.get("title") or "").replace("\n", " ").strip(),
                    "organizer": org.get("name"),
                    "unp": org.get("unp"),
                    "location": org.get("location"),
                    "sum_byn": lot.get("sumLot"),
                    "state": it.get("stateName"),
                    "created": fmt(created),
                    "deadline": fmt(ms_to_dt(it.get("requestDate"))),
                    "deadline_ts": it.get("requestDate"),
                    "url": CARD_URL.format(uuid=it.get("purchaseGiasId") or ""),
                }
            )
        if stop or data.get("last"):
            break
        page += 1

    log(f"    {keyword!r}: {len(rows)} за период (всего в архиве {total})")
    return rows


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="Разведка ГИАС по профилю компании")
    ap.add_argument("--days", type=int, default=90, help="глубина периода в днях")
    ap.add_argument("--profile", default=str(Path(__file__).parent / "profile_draft.json"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "out"))
    ap.add_argument("--page-size", type=int, default=50)
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--pause", type=float, default=0.4, help="пауза между запросами, сек")
    ap.add_argument("--proxy", default=None, help="напр. socks5://user:pass@host:port")
    args = ap.parse_args()

    def log(msg: str) -> None:
        print(msg, flush=True)

    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    groups: dict[str, list[str]] = profile["groups"]
    stop_words = [w.lower() for w in profile.get("stop_words", [])]
    service_markers = [w.lower() for w in profile.get("service_markers", [])]
    supply_markers = [w.lower() for w in profile.get("supply_markers", [])]

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    now = datetime.now(timezone.utc)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"ГИАС · период с {fmt(cutoff)} по {fmt(now)} ({args.days} дн.)")
    if args.proxy:
        log(f"через прокси {args.proxy.split('@')[-1]}")

    gias = Gias(proxy=args.proxy, pause=args.pause)
    all_rows: list[dict] = []
    for group, keywords in groups.items():
        log(f"  [{group}]")
        for kw in keywords:
            all_rows.extend(collect(gias, kw, group, cutoff, args.page_size,
                                    args.max_pages, log))

    # Дедупликация: одна закупка попадает по нескольким ключевым словам.
    uniq: dict[str, dict] = {}
    for r in all_rows:
        pid = r["purchase_id"]
        if pid in uniq:
            uniq[pid]["keyword"] += f"; {r['keyword']}"
        else:
            uniq[pid] = dict(r)

    # Заголовок закупки часто generic («Запчасти», «Аукцион», «Закупка из одного
    # источника») — поиск ГИАС попадает по составу лотов. Поэтому релевантность
    # считается на уровне лота, а для этого нужна карточка закупки.
    log("")
    log(f"Загрузка карточек: {len(uniq)} шт.")
    all_tokens = {kw: kw_tokens(kw) for kws in groups.values() for kw in kws}
    kw_group = {kw: g for g, kws in groups.items() for kw in kws}
    lot_rows: list[dict] = []
    no_card = 0
    for i, (pid, r) in enumerate(uniq.items(), 1):
        if i % 50 == 0:
            log(f"  {i}/{len(uniq)}")
        card = gias.card(pid)
        if not card:
            no_card += 1
            continue
        for lot in card.get("lots") or []:
            lt = lot.get("titleLot") or ""
            tn = norm(lt)
            hits = [kw for kw, toks in all_tokens.items() if toks and kw_matches(tn, toks)]
            if not hits:
                continue
            unit = lot.get("unit") or {}
            lot_rows.append(
                {
                    **{k: v for k, v in r.items() if k != "keyword"},
                    "group": kw_group[hits[0]],
                    "keyword": "; ".join(hits),
                    "lot_number": lot.get("lotNumber"),
                    "lot_title": lt.replace("\n", " ").strip(),
                    "okpb": ",".join(lot.get("codeOKPB") or []),
                    "volume": lot.get("volume"),
                    "unit": unit.get("name"),
                    "lot_price": lot.get("price"),
                    "delivery": (lot.get("deliveryLot") or "").replace("\n", " "),
                }
            )
    log(f"Карточек не получено: {no_card}")
    log(f"Лотов с попаданием по профилю: {len(lot_rows)}")

    rows = lot_rows

    def classify(title: str) -> str:
        t = (title or "").lower()
        if any(sw in t for sw in stop_words):
            return "stop"
        if any(m in t for m in supply_markers):
            return "supply"
        if any(m in t for m in service_markers):
            return "service"
        return "supply"

    for r in rows:
        r["kind"] = classify(f"{r['lot_title']} {r['title']}")

    dropped = [r for r in rows if r["kind"] == "stop"]
    services = [r for r in rows if r["kind"] == "service"]
    kept = [r for r in rows if r["kind"] == "supply"]

    now_ms = now.timestamp() * 1000
    active = [r for r in kept if r["deadline_ts"] and r["deadline_ts"] > now_ms]
    with_sum = [r for r in kept if r["lot_price"]]
    weeks = args.days / 7

    csv_path = out_dir / f"gias_{args.days}d.csv"
    cols = ["kind", "group", "keyword", "lot_title", "okpb", "volume", "unit",
            "lot_price", "number", "title", "organizer", "unp", "location",
            "delivery", "sum_byn", "state", "created", "deadline", "url",
            "purchase_id", "lot_number"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(kept + services,
                           key=lambda r: (r["kind"], r["created"]), reverse=True))

    by_group: dict[str, int] = defaultdict(int)
    for r in kept:
        by_group[r["group"]] += 1

    summary = {
        "source": "gias.by",
        "period_days": args.days,
        "period_from": fmt(cutoff),
        "period_to": fmt(now),
        "api_calls": gias.calls,
        "hits_raw": len(all_rows),
        "unique_purchases": len(uniq),
        "unique": len(rows),
        "dropped_by_stopwords": len(dropped),
        "services_excluded": len(services),
        "relevant": len(kept),
        "per_week": round(len(kept) / weeks, 1),
        "services_per_week": round(len(services) / weeks, 1),
        "active_now": len(active),
        "with_price": len(with_sum),
        "avg_lot_price_byn": round(
            sum(r["lot_price"] for r in with_sum) / len(with_sum), 2
        ) if with_sum else None,
        "median_lot_price_byn": sorted(r["lot_price"] for r in with_sum)[len(with_sum) // 2]
        if with_sum else None,
        "by_group": dict(sorted(by_group.items(), key=lambda kv: -kv[1])),
        "csv": str(csv_path),
    }
    (out_dir / f"gias_{args.days}d_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log("")
    log("=" * 62)
    log(f"Запросов к API:        {gias.calls}")
    log(f"Попаданий (с дублями): {len(all_rows)}")
    log(f"Уникальных закупок:    {len(uniq)}")
    log(f"Лотов по профилю:      {len(rows)}")
    log(f"Отсеяно стоп-словами:  {len(dropped)}")
    log(f"Работы и услуги:       {len(services)}  ({summary['services_per_week']} в неделю)")
    log(f"ПОСТАВКИ (профиль):    {len(kept)}  →  {summary['per_week']} лотов в неделю")
    log(f"Из них активных:       {len(active)}")
    if summary["avg_lot_price_byn"]:
        log(f"Цена лота BYN:         средняя {summary['avg_lot_price_byn']}, "
            f"медиана {summary['median_lot_price_byn']}")
    log("")
    for g, n in summary["by_group"].items():
        log(f"  {n:>5}  {g}")
    log("=" * 62)
    log(f"CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
