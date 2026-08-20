"""Источник: ГИАС (gias.by) — госзакупки Республики Беларусь.

API внутренний, официальной документации нет — контракт снят с сетевых запросов
самого сайта 20.08.2026 и может измениться без предупреждения. Поэтому форма
ответа проверяется явно (`_check_schema`), и при расхождении сбор падает громко,
а не возвращает тихую пустоту.

    POST /search/api/v1/search/purchases
         {"page", "pageSize", "contextTextSearch", "sortField", "sortOrder"}
    GET  /purchase/api/v1/purchase/{uuid}   → лоты, сроки, links[] с документацией

Авторизация не нужна, прокси не нужен. Сами файлы документации лежат уже на
goszakupki.by, и вот туда доступ может быть закрыт — этим занимается documents.py.
"""
from __future__ import annotations

import time

import requests

BASE = "https://gias.by"
SEARCH = f"{BASE}/search/api/v1/search/purchases"
CARD = f"{BASE}/purchase/api/v1/purchase/{{uuid}}"
PAGE = f"{BASE}/#/purchase/current/{{uuid}}"

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-cache",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
}

SEARCH_FIELDS = {"publicPurchaseNumber", "title", "dtCreate", "purchaseGiasId"}
CARD_FIELDS = {"lots", "stateName", "organizator"}

# Состояния, в которых ещё можно подать предложение. Названия приходят строкой;
# числовой код меняется между типами процедур, поэтому опираемся на название.
ACTIVE_STATES = {
    "подача предложений",
    "подача документов/сведений",
}


class SchemaChanged(RuntimeError):
    """Ответ площадки не той формы, которую ожидает код."""


class Gias:
    name = "gias"

    def __init__(self, proxy: str = "", pause: float = 0.4, timeout: int = 30):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.pause = pause
        self.timeout = timeout
        self.calls = 0

    # --- транспорт -------------------------------------------------------

    def _request(self, method: str, url: str, attempts: int = 4, **kw):
        last = None
        for i in range(attempts):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
                self.calls += 1
                if r.status_code == 200:
                    time.sleep(self.pause)
                    return r.json()
                if r.status_code == 404:
                    return None
                last = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (i + 1))
        raise RuntimeError(f"ГИАС не ответил после {attempts} попыток. {last}")

    @staticmethod
    def _check_schema(sample: dict, required: set[str], where: str) -> None:
        missing = required - set(sample.keys())
        if missing:
            raise SchemaChanged(
                f"ГИАС: изменилась форма ответа ({where}), нет полей {sorted(missing)}. "
                "Проверьте контракт API — код опирается на неофициальный интерфейс.")

    # --- операции --------------------------------------------------------

    def search(self, query: str, page: int = 0, page_size: int = 50) -> dict:
        data = self._request("POST", SEARCH, json={
            "page": page, "pageSize": page_size, "contextTextSearch": query,
            "sortField": "dtCreate", "sortOrder": "DESC",
        })
        content = (data or {}).get("content") or []
        if content:
            self._check_schema(content[0], SEARCH_FIELDS, "поиск")
        return data or {}

    def find_items(self, query: str, cutoff_ms: float, page_size: int = 50,
                   max_pages: int = 40) -> list[dict]:
        """Строки выдачи по закупкам новее cutoff_ms.

        Выдача отсортирована по dtCreate убыванию, поэтому как только ушли за
        границу окна — листать дальше незачем.

        Возвращаются строки целиком, а не идентификаторы: в них уже есть
        stateName и requestDate, и это позволяет отсеять неактуальное до
        загрузки карточек. Карточка стоит отдельного запроса, а закрытых
        процедур в выдаче кратно больше, чем открытых.
        """
        found: list[dict] = []
        for page in range(max_pages):
            data = self.search(query, page, page_size)
            content = data.get("content") or []
            if not content:
                break
            for item in content:
                if (item.get("dtCreate") or 0) < cutoff_ms:
                    return found
                found.append(item)
            if data.get("last"):
                break
        return found

    def card(self, uuid: str) -> dict | None:
        data = self._request("GET", CARD.format(uuid=uuid), attempts=3)
        if data:
            self._check_schema(data, CARD_FIELDS, "карточка закупки")
        return data


def parse_card(card: dict, uuid: str) -> tuple[dict, list[dict], list[dict]]:
    """Карточка ГИАС → строки для базы: закупка, её лоты, её файлы."""
    org = card.get("organizator") or {}
    purchase = {
        "id": uuid,
        "source": Gias.name,
        "number": card.get("publicPurchaseNumber"),
        "title": (card.get("title") or "").strip(),
        "state": card.get("stateName"),
        "tender_form": card.get("tenderFormName"),
        "organizer": org.get("name"),
        "unp": org.get("unp"),
        "location": org.get("location"),
        "sum_lot": (card.get("sumLot") or {}).get("sumLot"),
        "created_ms": card.get("dtCreate"),
        "updated_ms": card.get("dtUpdate"),
        "deadline_ms": card.get("requestDate"),
        "auction_url": card.get("auctionUrl"),
        "page_url": PAGE.format(uuid=uuid),
        "days_left": None,
    }

    lots = []
    for lot in card.get("lots") or []:
        unit = lot.get("unit") or {}
        lots.append({
            "id": lot.get("id"),
            "purchase_id": uuid,
            "lot_number": lot.get("lotNumber"),
            "title": (lot.get("titleLot") or "").replace("\n", " ").strip(),
            "okpb": ",".join(lot.get("codeOKPB") or []),
            "volume": lot.get("volume"),
            "unit": unit.get("name"),
            "price": lot.get("price"),
            "delivery": (lot.get("deliveryLot") or "").replace("\n", " ").strip(),
            "state": lot.get("stateName"),
        })

    files = []
    for i, link in enumerate(card.get("links") or []):
        files.append({
            "purchase_id": uuid,
            "idx": i,
            "name": link.get("name"),
            "url": link.get("link"),
            "local": None,
            "status": "new",
        })
    return purchase, lots, files


def is_active_state(state: str | None) -> bool:
    return (state or "").strip().lower() in ACTIVE_STATES


class GiasSource:
    """Источник в общем интерфейсе (app/sources/base.py)."""

    name = "gias"
    title = "ГИАС — госзакупки РБ"

    def harvest(self, profile, cfg: dict, progress=None, cancelled=None):
        from datetime import datetime, timedelta, timezone

        from app import calendar_by

        def say(msg: str) -> None:
            if progress:
                progress(f"ГИАС · {msg}")

        def stop() -> bool:
            return bool(cancelled and cancelled())

        api = Gias(proxy=cfg.get("proxy", ""),
                   pause=float(cfg.get("request_pause", 0.4)))
        window = int(cfg.get("window_days", 30))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).timestamp() * 1000

        queries = [text for text, _ in profile.queries()]
        queries += [o["query"] for o in profile.organizers]

        # Выдача поиска уже содержит состояние и срок подачи, поэтому неактуальное
        # отсеивается до загрузки карточек: закрытых процедур кратно больше, а
        # карточка каждой стоит отдельного запроса.
        candidates: dict[str, dict] = {}
        skipped = 0
        for i, query in enumerate(queries, 1):
            if stop():
                return
            say(f"поиск {i}/{len(queries)}: {query}")
            for item in api.find_items(query, cutoff):
                if not is_active_state(item.get("stateName")):
                    continue
                if not calendar_by.is_actual(item.get("requestDate"), cfg):
                    skipped += 1
                    continue
                candidates.setdefault(item["purchaseGiasId"], item)

        say(f"актуальных {len(candidates)}, отсеяно по сроку {skipped}")
        for i, uuid in enumerate(candidates, 1):
            if stop():
                return
            if i % 10 == 0 or i == 1:
                say(f"карточки {i}/{len(candidates)}")
            card = api.card(uuid)
            if not card:
                continue
            purchase, lots, files = parse_card(card, uuid)
            purchase["days_left"] = calendar_by.days_left(purchase["deadline_ms"], cfg)
            yield purchase, lots, files

        self.calls = api.calls
