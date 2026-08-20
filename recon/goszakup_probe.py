#!/usr/bin/env python3
"""Разведка goszakup.gov.kz (Казахстан) по профилю ООО «Промтехснаб».

Этап 0 плана, вторая площадка. Логика совпадает с gias_probe.py, но здесь
матчинг сразу идёт по лотам — реестр лотов отдаётся отдельным эндпоинтом,
дозагружать карточки не нужно.

ВНИМАНИЕ: скрипт не проверен на живом API — на момент написания токена нет.
Проверено только то, что сервис жив и отвечает 401 без авторизации:
    GET https://ows.goszakup.gov.kz/v2/lots?limit=1  ->  401 Unauthorized

Токен запрашивается бесплатно письмом в Департамент цифровизации Минфина РК
(через АО «Центр электронных финансов»), срок действия 1 год, далее
перевыпускается из кабинета участника.

Запуск:
    set GOSZAKUP_TOKEN=...
    python recon/goszakup_probe.py --days 90
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from gias_probe import kw_matches, kw_tokens, norm  # общий матчинг

BASE = "https://ows.goszakup.gov.kz"
LOTS = f"{BASE}/v3/lots"
LOT_URL = "https://goszakup.gov.kz/ru/announce/index/{trd_buy_id}"


class Goszakup:
    def __init__(self, token: str, pause: float = 0.3, timeout: int = 30):
        self.s = requests.Session()
        self.s.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )
        self.pause = pause
        self.timeout = timeout
        self.calls = 0

    def lots_page(self, params: dict, attempts: int = 4) -> dict:
        last = None
        for i in range(attempts):
            try:
                r = self.s.get(LOTS, params=params, timeout=self.timeout)
                self.calls += 1
                if r.status_code == 200:
                    time.sleep(self.pause)
                    return r.json()
                if r.status_code == 401:
                    raise SystemExit(
                        "401 Unauthorized — токен не принят. Проверьте GOSZAKUP_TOKEN "
                        "и срок его действия."
                    )
                last = f"HTTP {r.status_code}: {r.text[:200]}"
            except SystemExit:
                raise
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (i + 1))
        raise RuntimeError(f"goszakup не ответил после {attempts} попыток. {last}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="Разведка goszakup.gov.kz")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--profile", default=str(Path(__file__).parent / "profile_draft.json"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "out"))
    ap.add_argument("--limit", type=int, default=500, help="размер страницы")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--token", default=os.environ.get("GOSZAKUP_TOKEN"))
    args = ap.parse_args()

    if not args.token:
        print("Нет токена. Задайте GOSZAKUP_TOKEN или передайте --token.")
        print("Токен запрашивается письмом в Минфин РК, бесплатно, срок 1 год.")
        return 2

    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    groups: dict[str, list[str]] = profile["groups"]
    service_markers = [w.lower() for w in profile.get("service_markers", [])]
    supply_markers = [w.lower() for w in profile.get("supply_markers", [])]

    all_tokens = {kw: kw_tokens(kw) for kws in groups.values() for kw in kws}
    kw_group = {kw: g for g, kws in groups.items() for kw in kws}

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    api = Goszakup(args.token)
    rows: list[dict] = []
    seen: set = set()
    page = 0
    scanned = 0
    params = {"limit": args.limit, "sort": "-id"}

    print(f"goszakup.gov.kz · период с {cutoff:%Y-%m-%d}, страниц максимум {args.max_pages}")
    while page < args.max_pages:
        data = api.lots_page(params)
        items = data.get("items") or []
        if not items:
            break
        stop = False
        for it in items:
            scanned += 1
            created = it.get("index_date") or it.get("last_update_date")
            if created:
                try:
                    dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        stop = True
                        break
                except ValueError:
                    pass
            text = norm(f"{it.get('name_ru') or ''} {it.get('description_ru') or ''}")
            hits = [kw for kw, toks in all_tokens.items() if toks and kw_matches(text, toks)]
            if not hits:
                continue
            lid = it.get("id")
            if lid in seen:
                continue
            seen.add(lid)
            title = it.get("name_ru") or ""
            t = title.lower()
            kind = ("supply" if any(m in t for m in supply_markers)
                    else "service" if any(m in t for m in service_markers)
                    else "supply")
            rows.append({
                "kind": kind,
                "group": kw_group[hits[0]],
                "keyword": "; ".join(hits),
                "lot_number": it.get("lot_number"),
                "lot_title": title.replace("\n", " ").strip(),
                "description": (it.get("description_ru") or "").replace("\n", " ")[:400],
                "amount_kzt": it.get("amount"),
                "count": it.get("count"),
                "customer": it.get("customer_name_ru"),
                "customer_bin": it.get("customer_bin"),
                "created": str(created)[:10],
                "url": LOT_URL.format(trd_buy_id=it.get("trd_buy_id")),
            })
        nxt = (data.get("next_page") or "").strip()
        if stop or not nxt:
            break
        params = {"limit": args.limit, "sort": "-id",
                  "from_id": items[-1].get("id")}
        page += 1

    kept = [r for r in rows if r["kind"] == "supply"]
    services = [r for r in rows if r["kind"] == "service"]
    weeks = args.days / 7

    csv_path = out_dir / f"goszakup_{args.days}d.csv"
    cols = ["kind", "group", "keyword", "lot_title", "description", "amount_kzt",
            "count", "customer", "customer_bin", "created", "url", "lot_number"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    by_group: dict[str, int] = defaultdict(int)
    for r in kept:
        by_group[r["group"]] += 1

    summary = {
        "source": "goszakup.gov.kz",
        "period_days": args.days,
        "api_calls": api.calls,
        "lots_scanned": scanned,
        "matched": len(rows),
        "services_excluded": len(services),
        "relevant": len(kept),
        "per_week": round(len(kept) / weeks, 1),
        "by_group": dict(sorted(by_group.items(), key=lambda kv: -kv[1])),
        "csv": str(csv_path),
    }
    (out_dir / f"goszakup_{args.days}d_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("=" * 62)
    print(f"Просмотрено лотов:  {scanned}")
    print(f"Совпало по профилю: {len(rows)}")
    print(f"Работы и услуги:    {len(services)}")
    print(f"ПОСТАВКИ:           {len(kept)}  →  {summary['per_week']} лотов в неделю")
    for g, n in summary["by_group"].items():
        print(f"  {n:>5}  {g}")
    print("=" * 62)
    print(f"CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
