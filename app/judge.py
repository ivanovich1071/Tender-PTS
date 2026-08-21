"""Второй уровень отбора: модель решает, профильный ли лот.

Зачем он нужен. Правила и словарь ловят номенклатуру по словам и на этом
заканчиваются, а решает смысл: «поддон деревянный» — не наше, «поддон печи
закалочной» — наше; «форсунка ТНВД» — не наше, «форсунка закалочной камеры» —
наше. Слово одно и то же. Ровно так же бесполезно искать точное совпадение с
названием чертежа: часть деталей повторяется из тендера в тендер, часть
выпадает раз в год, и списком их не покрыть.

Поэтому модели даётся не словарь, а признак производства из профиля
(`capability_note`): ПТС делает металлические детали по чертежу заказчика.
Вопрос к модели один — может ли ПТС это изготовить и поставить.

Три свойства, без которых это не работало бы:

1. **Кэш.** Вердикт вешается на содержание лота (заказчик + название + ОКПБ),
   а не на его номер, поэтому повторная закупка той же детали не стоит ни
   одного запроса, а поправка оператора переживает пересбор.
2. **Примеры оператора.** В запрос уходят прошлые решения человека — что взяли,
   что пропустили, что выбросили из списка. Модель учится на них, а не на
   придуманных правилах.
3. **Отказ безопасен.** Нет ключа, кончился лимит, молчит сеть — лот остаётся с
   решением правил и пометкой «моделью не проверено». Ошибка идёт в журнал,
   сбор доходит до конца.
"""
from __future__ import annotations

import json
import os
import re

import requests

from app import logs, store

API = "https://openrouter.ai/api/v1/chat/completions"
MODELS_API = "https://openrouter.ai/api/v1/models"
TIMEOUT = 120

VERDICTS = {"fit", "off", "maybe"}
RU = {"fit": "профиль", "off": "мимо профиля", "maybe": "сомнительно"}

SYSTEM = """Ты отбираешь тендерные лоты для машиностроительного предприятия.

ЧТО ПРЕДПРИЯТИЕ ДЕЛАЕТ
{capability}

ЧЕГО ПРЕДПРИЯТИЕ НЕ ПОСТАВЛЯЕТ
{never}

КАК РЕШАТЬ
Вопрос один: может ли завод изготовить это по чертежу или поставить как
изделие своего профиля. Название товара само по себе ничего не решает —
одно и то же слово бывает и нашим, и чужим:
{pairs}

Не требуй точного совпадения с известной номенклатурой: часть деталей
повторяется из года в год, часть встречается один раз. Смотри на суть
изделия, а не на знакомое слово.

ОТВЕТ
Только JSON-массив, без пояснений вокруг. По объекту на каждый лот:
{{"n": номер, "v": "fit" | "off" | "maybe", "why": "до восьми слов, по-русски"}}
fit — профильное, off — мимо, maybe — по названию не понять.
"""


def mask_key(key: str) -> str:
    """Ключ в журнал не попадает: журнал пересылают."""
    if not key:
        return "(не задан)"
    return f"{key[:7]}…{key[-4:]}"


def api_key(cfg: dict) -> str:
    """Ключ OpenRouter: сначала из настроек, потом из .env.

    Два места нарочно. `.env` — привычное: его правят в редакторе, не запуская
    приложение, и он же переживает переустановку. Поле в настройках сильнее,
    чтобы вписанное руками не оказалось молча проигнорированным; интерфейс при
    этом показывает, откуда ключ взят.
    """
    return ((cfg.get("openrouter_key") or "").strip()
            or os.environ.get("OPENROUTER_API_KEY", "").strip())


def key_source(cfg: dict) -> str:
    if (cfg.get("openrouter_key") or "").strip():
        return "настройки"
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        return ".env"
    return ""


def configured(cfg: dict) -> bool:
    return bool(api_key(cfg)) and bool(cfg.get("judge"))


def _proxies(cfg: dict) -> dict | None:
    proxy = (cfg.get("proxy") or "").strip()
    return {"http": proxy, "https": proxy} if proxy else None


def free_models(cfg: dict) -> list[dict]:
    """Бесплатные модели OpenRouter — списком с самой площадки.

    Вписывать их в код бессмысленно: набор меняется чаще, чем выходят наши
    версии, и вчерашняя бесплатная модель сегодня платная.
    """
    r = requests.get(MODELS_API, timeout=30, proxies=_proxies(cfg))
    r.raise_for_status()
    out = []
    for item in r.json().get("data", []):
        pricing = item.get("pricing") or {}
        try:
            free = float(pricing.get("prompt", 1)) == 0 and \
                float(pricing.get("completion", 1)) == 0
        except (TypeError, ValueError):
            free = False
        if free:
            out.append({"id": item.get("id"), "name": item.get("name")})
    return sorted(out, key=lambda x: x["id"] or "")


def build_prompt(prof, examples: list[dict]) -> str:
    raw = prof.raw
    never = "\n".join(f"— {x}" for x in raw.get("never_supply", [])) or "— услуги и работы"
    pairs = "\n".join(f"— {p}" for p in raw.get("judge_pairs", []))
    system = SYSTEM.format(
        capability=raw.get("capability_note", ""), never=never, pairs=pairs)
    if examples:
        lines = "\n".join(
            f"— «{e['title']}» ({e.get('organizer') or 'заказчик не указан'}) → {e['verdict']}"
            for e in examples)
        system += ("\nРЕШЕНИЯ ОПЕРАТОРА ПО ПРОШЛЫМ ЛОТАМ — держись их:\n" + lines + "\n")
    return system


def ask(cfg: dict, system: str, batch: list[dict]) -> list[dict]:
    """Один запрос по пачке лотов. Возвращает разобранный ответ модели."""
    listing = []
    for n, lot in enumerate(batch, 1):
        parts = [f"{n}. лот: {lot['title']}"]
        if lot.get("purchase_title") and lot["purchase_title"] != lot["title"]:
            parts.append(f"закупка: {lot['purchase_title']}")
        if lot.get("okpb"):
            parts.append(f"ОКПБ: {lot['okpb']}")
        if lot.get("organizer"):
            parts.append(f"заказчик: {lot['organizer']}")
        if lot.get("files"):
            parts.append("файлы: " + ", ".join(lot["files"][:6]))
        listing.append("   ".join(parts))

    body = {
        "model": cfg.get("model"),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(listing)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key(cfg)}",
        "Content-Type": "application/json",
        "X-Title": "Tender-PTS",
    }
    r = requests.post(API, json=body, headers=headers, timeout=TIMEOUT,
                      proxies=_proxies(cfg))
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter ответил {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:200])
    text = data["choices"][0]["message"]["content"]
    return parse_answer(text, len(batch))


def parse_answer(text: str, count: int) -> list[dict]:
    """Разобрать ответ модели.

    Модель охотно оборачивает JSON в ```-блок или предисловие, поэтому берётся
    первый же массив в тексте. Если не разобралось — это ошибка запроса, а не
    повод молча выбросить пачку лотов.
    """
    match = re.search(r"\[.*\]", text or "", re.S)
    if not match:
        raise ValueError(f"в ответе нет JSON: {(text or '')[:120]}")
    items = json.loads(match.group(0))
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        verdict = str(item.get("v", "")).strip().lower()
        if not 1 <= n <= count or verdict not in VERDICTS:
            continue
        out.append({"n": n, "v": verdict,
                    "why": str(item.get("why", "")).strip()[:120]})
    return out


def review(conn, prof, cfg: dict, progress=None) -> dict:
    """Проставить вердикты всем лотам без вердикта. Наружу не бросает."""
    stats = {"judged": 0, "from_cache": 0, "off": 0, "calls": 0, "errors": []}

    rows = [dict(r) for r in conn.execute(
        """SELECT l.id, l.title, l.okpb, p.title AS purchase_title, p.organizer,
                  p.id AS purchase_id
             FROM lots l JOIN purchases p ON p.id = l.purchase_id
            WHERE l.kind = 'supply' AND l.verdict IS NULL""")]
    if not rows:
        return stats

    cache = store.verdict_cache(conn)
    pending = []
    for row in rows:
        key = store.lot_key(row["organizer"], row["title"], row["okpb"])
        row["key"] = key
        hit = cache.get(key)
        if hit:
            store.apply_verdict(conn, row["id"], hit["verdict"], hit["why"],
                                hit["who"])
            stats["from_cache"] += 1
            if hit["verdict"] == "off":
                stats["off"] += 1
        else:
            pending.append(row)
    conn.commit()

    if not pending:
        logs.log.info("отбор: все %s лотов взяты из кэша вердиктов",
                      stats["from_cache"])
        return stats

    if not configured(cfg):
        logs.log.warning(
            "отбор: модель не проверяла %s лотов — %s", len(pending),
            "ключ OpenRouter не задан" if not api_key(cfg)
            else "проверка моделью выключена в настройках")
        for row in pending:
            store.apply_verdict(conn, row["id"], "maybe", "моделью не проверено", "")
        conn.commit()
        return stats

    for row in pending:
        row["files"] = [f["name"] for f in store.files_of(conn, row["purchase_id"])]

    system = build_prompt(prof, store.examples(conn))
    size = max(1, int(cfg.get("judge_batch") or 20))
    model = cfg.get("model") or ""
    logs.log.info("отбор: моделью %s, лотов %s, из кэша %s",
                  model, len(pending), stats["from_cache"])

    for start in range(0, len(pending), size):
        batch = pending[start:start + size]
        if progress:
            progress(f"отбор моделью {min(start + size, len(pending))}/{len(pending)}")
        try:
            answers = ask(cfg, system, batch)
            stats["calls"] += 1
        except Exception as e:
            message = f"отбор: пачка {start // size + 1} не проверена — {type(e).__name__}: {str(e)[:160]}"
            logs.log.error(message)
            stats["errors"].append(message)
            for row in batch:
                store.apply_verdict(conn, row["id"], "maybe",
                                    "моделью не проверено", "")
            conn.commit()
            continue

        answered = set()
        for item in answers:
            row = batch[item["n"] - 1]
            answered.add(item["n"])
            store.save_verdict(conn, row["key"], item["v"], item["why"],
                               "модель", model, row["title"] or "",
                               row["organizer"] or "")
            store.apply_verdict(conn, row["id"], item["v"], item["why"], "модель")
            stats["judged"] += 1
            if item["v"] == "off":
                stats["off"] += 1
                logs.log.info("  мимо · %s · %s",
                              (row["title"] or "")[:70], item["why"])
        for n, row in enumerate(batch, 1):
            if n not in answered:
                store.apply_verdict(conn, row["id"], "maybe",
                                    "модель не ответила по этому лоту", "")
        conn.commit()

    logs.log.info("отбор: проверено моделью %s, мимо профиля %s, запросов %s",
                  stats["judged"], stats["off"], stats["calls"])
    return stats
