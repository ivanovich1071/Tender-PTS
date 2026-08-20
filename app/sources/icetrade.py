"""Источник: icetrade.by — закупки за счёт собственных средств и в строительстве.

Почему это важнее ГИАС для ПТС. ГИАС — бюджетная сфера. Крупные заводы
(БМЗ, БЕЛАЗ, ГродноАзот, Беларуськалий, Гомельский химический) закупают за
собственные средства, и это идёт сюда. Проверено 20.08.2026: у БМЗ в ГИАС
100 закупок за шесть лет, и то тепловизоры и дизтопливо, у Гомельского
химического последняя закупка в ГИАС — за 2023 год. Зато на icetrade у обоих
в тот же день висели живые процедуры ровно по профилю ПТС.

Разметка снята с настоящих страниц (см. tests/fixtures/icetrade) и на них же
проверяется. Ключевое, что из неё следует:

* Поля карточки помечены устойчивыми классами `af-*`, значение всегда в `td.afv`.
* Лоты приходят прямо в HTML, догружать их запросом `lots/view` не нужно.
* Документация открыта: страницы сохранены незалогиненными, а ссылки на файлы
  обычные — `/auction/getFile/auction/{id}?f=detail&n={N}`. Учётная запись не нужна.
* Поиск по слову ищет только по краткому описанию закупки. Запрос «шестерня»
  дал четыре результата, и все мимо — запчасти к тракторам для птицефабрик.
  Номенклатура живёт в лотах, поэтому основной обход идёт по отрасли и заказчику,
  а отбор — по лотам карточки, как и в ГИАС.

Две особенности доступа, обе подтверждены замером:

1. **Гео-блокировка.** С адреса вне Беларуси сайт отвечает 403 на любой путь,
   включая robots.txt. Нужен прокси с белорусским адресом — он задаётся в
   настройках приложения и применяется здесь.
2. **Неполная цепочка сертификата.** Сервер не отдаёт промежуточный
   сертификат, из-за чего обычная проверка падает с
   SSLCertVerificationError, хотя рукопожатие проходит. Поэтому при ошибке
   проверки делается одна повторная попытка без неё, и это записывается в
   предупреждения. Данные тут публичные, ничего секретного не передаётся, но
   молча отключать проверку нельзя — оператор должен знать.
"""
from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests

from app import calendar_by, matching
from app.sources.base import SourceError

BASE = "https://icetrade.by"
SEARCH_PATH = "/search/auctions"
CARD_PATH = "/tenders/all/view/{id}"
INDUSTRIES_PATH = "/industries/select_multi?ajax=1"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Типы процедур и области — списки взяты из собственных ссылок меню площадки,
# то есть это в точности то, что она считает «всеми закупками Беларуси».
# Marketing и MarketingForPrice — процедуры по постановлению № 168: цены в них
# часто нет, но именно они прямее всего ведут к запросу коммерческого предложения.
TYPES = ("Trade", "eTrade", "Request", "singleSource", "Auction", "Other",
         "contractingTrades", "socialOrder", "negotiations", "Limited",
         "twoStageFirst", "twoStageSecond", "Marketing", "MarketingForPrice")
REGIONS = (1, 2, 3, 4, 5, 6, 7)

DEFAULTS = {
    "base": BASE,
    "allow_insecure_tls": True,
    # Окно берётся по сроку подачи, а не по дате размещения. Закупка Гомельского
    # химического завода размещена 25.06, а предложения принимаются до 31.08:
    # при окне по дате размещения она бы не нашлась.
    "deadline_window_days": 90,
    "on_page": 100,
    "max_pages": 20,          # предел листания одной выдачи
    "max_cards": 400,
    # Отрасли выбираются по вхождению названия: идентификаторы у площадки свои и
    # подгружаются с её же страницы выбора отраслей.
    "industry_match": ["машиностроен", "металлург", "химическ"],
    "industries": [],
    "keyword_pass": True,
}


class Icetrade:
    name = "icetrade"
    title = "icetrade.by — закупки за собственные средства"

    def __init__(self, cfg: dict):
        self.cfg = {**DEFAULTS, **(cfg.get("icetrade") or {})}
        self.proxy = (cfg.get("proxy") or "").strip()
        self.pause = float(cfg.get("request_pause", 0.4))
        self.warnings: list[str] = []
        self.calls = 0
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        if self.proxy:
            self.s.proxies = {"http": self.proxy, "https": self.proxy}

    # --- транспорт -------------------------------------------------------

    def get(self, url: str, timeout: int = 30) -> str:
        """GET с одной уступкой: повтор без проверки TLS при битой цепочке."""
        if self.calls:
            time.sleep(self.pause)
        try:
            r = self.s.get(url, timeout=timeout)
        except requests.exceptions.SSLError:
            if not self.cfg.get("allow_insecure_tls"):
                raise SourceError(
                    "icetrade.by: сервер не отдаёт промежуточный сертификат. "
                    "Разрешите allow_insecure_tls в настройках источника или "
                    "добавьте корневой сертификат НЦЭУ в доверенные.")
            self._warn("проверка TLS отключена: сервер не отдал промежуточный сертификат")
            r = self.s.get(url, timeout=timeout, verify=False)
        except (requests.exceptions.ConnectTimeout,
                requests.exceptions.ConnectionError) as e:
            raise SourceError(
                "icetrade.by недоступен. Площадка закрыта для адресов вне Беларуси — "
                f"нужен белорусский прокси в настройках. ({type(e).__name__})") from e

        self.calls += 1
        if r.status_code == 403:
            raise SourceError(
                "icetrade.by ответил 403: адрес не из Беларуси. Укажите белорусский "
                "прокси в настройках приложения.")
        if r.status_code != 200:
            raise SourceError(f"icetrade.by ответил {r.status_code}")
        return r.text

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    # --- проверка доступа ------------------------------------------------

    def check(self) -> dict:
        """Доступна ли площадка. Вызывается отдельно, чтобы сказать это внятно."""
        try:
            page = self.get(self.cfg["base"] + "/", timeout=25)
        except SourceError as e:
            return {"ok": False, "reason": str(e), "proxy": bool(self.proxy)}
        return {"ok": True, "size": len(page), "proxy": bool(self.proxy),
                "warnings": self.warnings}

    # --- поиск -----------------------------------------------------------

    def search_url(self, **fields) -> str:
        """Адрес поиска. Набор полей взят из собственной формы площадки."""
        today = datetime.now(calendar_by.MINSK).date()
        horizon = today + timedelta(days=int(self.cfg["deadline_window_days"]))
        params = [
            ("search_text", fields.get("search_text", "")),
            ("search", "Найти"),
            ("zakup_type[1]", "1"),
            ("zakup_type[2]", "1"),
            ("auc_num", ""),
            ("okrb", fields.get("okrb", "")),
            ("company_title", fields.get("company_title", "")),
            ("industries", fields.get("industries", "")),
            ("establishment", "0"),
            ("created_from", ""),
            ("created_to", ""),
            ("request_end_from", today.strftime("%d.%m.%Y")),
            ("request_end_to", horizon.strftime("%d.%m.%Y")),
        ]
        params += [(f"t[{t}]", "1") for t in TYPES]
        params += [(f"r[{r}]", str(r)) for r in REGIONS]
        params += [("sort", "num:desc"), ("onPage", str(self.cfg["on_page"]))]
        page = int(fields.get("page", 1))
        if page > 1:
            params.append(("page", str(page)))
        return self.cfg["base"] + SEARCH_PATH + "?" + urlencode(params)

    def search(self, label: str, **fields) -> list[dict]:
        """Все строки выдачи по одному запросу, с листанием."""
        rows, total = parse_results(self.get(self.search_url(**fields)))
        seen = {row["id"] for row in rows}
        page = 1
        while total > len(seen) and rows and page < int(self.cfg["max_pages"]):
            page += 1
            more, _ = parse_results(self.get(self.search_url(page=page, **fields)))
            fresh = [row for row in more if row["id"] not in seen]
            if not fresh:
                # Площадка не показала следующую страницу: либо листание устроено
                # иначе, либо «Всего» считает не то. Молчать об этом нельзя —
                # часть закупок мы просто не увидим.
                self._warn(
                    f"«{label}»: площадка сообщила {total} закупок, а отдала "
                    f"{len(seen)}. Листание выдачи не сработало — часть пропущена.")
                break
            rows += fresh
            seen |= {row["id"] for row in fresh}
        if total > len(seen) and page >= int(self.cfg["max_pages"]):
            self._warn(
                f"«{label}»: выдача обрезана на {len(seen)} из {total} — упёрлись "
                "в предел страниц. Сузьте отрасли или поднимите его в настройках.")
        return rows

    def industries(self) -> dict[str, str]:
        """Дерево отраслей площадки: идентификатор → название."""
        return parse_industries(self.get(self.cfg["base"] + INDUSTRIES_PATH, timeout=25))

    # --- сбор ------------------------------------------------------------

    def harvest(self, profile, cfg: dict, progress=None, cancelled=None):
        def say(msg: str) -> None:
            if progress:
                progress(f"icetrade · {msg}")

        def stop() -> bool:
            return bool(cancelled and cancelled())

        candidates: dict[str, dict] = {}

        def take(label: str, **fields) -> None:
            rows = self.search(label, **fields)
            for row in rows:
                candidates.setdefault(row["id"], row)
            say(f"{label}: {len(rows)}")

        # 1. Отрасли — основной проход: номенклатура в описании закупки не видна,
        #    зато отрасль у машиностроительной закупки проставлена почти всегда.
        ids = [str(i) for i in (self.cfg.get("industries") or [])]
        if not ids and self.cfg.get("industry_match"):
            try:
                tree = self.industries()
                wanted = [w.lower() for w in self.cfg["industry_match"]]
                ids = [i for i, name in tree.items()
                       if any(w in name.lower() for w in wanted)]
                say(f"отраслей выбрано {len(ids)} из {len(tree)}")
            except SourceError as e:
                self._warn(
                    f"дерево отраслей не получено ({e}); проход по отраслям пропущен")
        if ids and not stop():
            take("отрасли", industries=",".join(ids))

        # 2. Заказчики под наблюдением — поимённо.
        for org in profile.organizers:
            if stop():
                break
            take(f"заказчик {org['query']}", company_title=org["query"])

        # 3. Слова профиля — узко и дёшево. Держится ради редких сплавов
        #    (хастеллой, инконель): по отрасли такую закупку не поймать.
        if self.cfg.get("keyword_pass"):
            queries = [text for text, _ in profile.queries()]
            for i, query in enumerate(queries, 1):
                if stop():
                    break
                if i % 20 == 0:
                    say(f"слова {i}/{len(queries)}")
                take(f"слово {query}", search_text=query)

        # Строка выдачи уже содержит срок подачи и заказчика, поэтому карточка
        # запрашивается только для того, что прошло календарь и стоп-слова:
        # каждая карточка — отдельный запрос, и их тут сотни.
        keep, skipped_deadline, skipped_stop = [], 0, 0
        for row in candidates.values():
            if not calendar_by.is_actual(row["deadline_ms"], cfg):
                skipped_deadline += 1
                continue
            if matching.classify_kind(profile, row["title"]) == "stop":
                skipped_stop += 1
                continue
            keep.append(row)

        limit = int(self.cfg["max_cards"])
        if len(keep) > limit:
            self._warn(
                f"кандидатов {len(keep)}, а предел за прогон — {limit}. Взяты самые "
                "срочные; поднимите предел в настройках или сузьте отрасли.")
            keep.sort(key=lambda row: row["deadline_ms"] or 0)
            keep = keep[:limit]

        say(f"кандидатов {len(keep)}, отсеяно по сроку {skipped_deadline}, "
            f"по стоп-словам {skipped_stop}")

        for i, row in enumerate(keep, 1):
            if stop():
                return
            if i % 10 == 0 or i == 1:
                say(f"карточки {i}/{len(keep)}")
            try:
                page = self.get(self.cfg["base"] + CARD_PATH.format(id=row["id"]))
                purchase, lots, files = parse_card(page, row["id"])
            except SourceError as e:
                # Одна испорченная карточка не должна ронять прогон, но и
                # исчезать молча не должна.
                self._warn(f"карточка {row['id']}: {e}")
                continue
            purchase["days_left"] = calendar_by.days_left(purchase["deadline_ms"], cfg)
            yield purchase, lots, files


# --- разбор страниц ------------------------------------------------------
#
# Разбор отделён от клиента намеренно: он проверяется на сохранённых страницах
# без сети (tests/fixtures/icetrade). Каждая функция громко падает, если не
# нашла того, на чём построена, — пустая карточка хуже честной ошибки.

TAG_RE = re.compile(r"(?s)<[^>]+>")
BR_RE = re.compile(r"(?is)<br\s*/?>")
SCRIPT_RE = re.compile(r"(?is)<script.*?</script>")
FIELD_RE = re.compile(r'(?is)<tr[^>]*class="[^"]*\baf-([a-z_]+)\b[^"]*"[^>]*>(.*?)</tr>')
AFV_RE = re.compile(r'(?is)<td[^>]*\bafv\b[^>]*>(.*?)</td>')
HREF_RE = re.compile(r'(?is)<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>')
LOTS_RE = re.compile(r'(?is)<table[^>]*id="lots_list"[^>]*>(.*?)</table>')
LOT_ROW_RE = re.compile(r'(?is)<tr[^>]*id="lotRow(\d+)"[^>]*>(.*?)</tr>')
SUB_ROW_RE = re.compile(
    r'(?is)<tr[^>]*class="[^"]*lotSubRow\s+lsr(\d+)[^"]*"[^>]*>(.*?)</tr>')
CELL_RE = re.compile(r"(?is)<t([dh])[^>]*>(.*?)</t[dh]>")
FILE_TD_RE = re.compile(r'(?is)<td[^>]*\baf-files\b[^>]*>(.*?)</td>')
H1_RE = re.compile(r"(?is)<h1[^>]*>(.*?)</h1>")
FORM_RE = re.compile(r'(?is)<tr[^>]*class="[^"]*\bfst\b[^"]*"[^>]*>(.*?)</tr>')
ROW_RE = re.compile(r'(?is)<tr[^>]*class="rw-(\d+)[^"]*"[^>]*>(.*?)</tr>')
TOTAL_RE = re.compile(r'(?is)<div class="total">\s*Всего:\s*(\d+)')
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def text_of(fragment: str) -> str:
    """Видимый текст куска разметки. Перевод строки там, где был <br>."""
    s = SCRIPT_RE.sub(" ", fragment)
    s = BR_RE.sub("\n", s)
    s = TAG_RE.sub(" ", s)
    s = html_mod.unescape(s).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in s.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def to_ms(text: str) -> int | None:
    """Дата площадки → миллисекунды эпохи. Без времени — конец дня по Минску."""
    m = DATE_RE.search(text or "")
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    when = datetime(int(year), int(month), int(day),
                    int(hour) if hour else 23, int(minute) if minute else 59,
                    tzinfo=calendar_by.MINSK)
    return int(when.timestamp() * 1000)


def to_number(text: str) -> float | None:
    """Число из ячейки. Ноль — это «цена не объявлена», а не бесплатно.

    У запросов цен по постановлению № 168 площадка пишет «0 BYN»: показывать
    оператору ноль хуже, чем честный прочерк.
    """
    s = (text or "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    value = float(m.group())
    return value or None


def parse_results(page: str) -> tuple[list[dict], int]:
    """Страница выдачи → строки и общее число закупок по мнению площадки."""
    if "auctions-list" not in page:
        if "search_text" in page:
            return [], 0        # форма поиска без результатов — это не поломка
        raise SourceError(
            "в ответе нет таблицы выдачи. Похоже, отдана не та страница — "
            "проверьте доступ и адрес поиска.")
    total = TOTAL_RE.search(page)
    rows = []
    for auc_id, body in ROW_RE.findall(page):
        cells = [cell for _, cell in CELL_RE.findall(body)]
        if len(cells) < 6:
            continue
        link = HREF_RE.search(cells[0])
        rows.append({
            "id": auc_id,
            "title": text_of(cells[0]).replace("\n", " "),
            "organizer": text_of(cells[1]),
            "country": text_of(cells[2]),
            "number": text_of(cells[3]),
            "price": to_number(text_of(cells[4])),
            "deadline_ms": to_ms(text_of(cells[5])),
            "page_url": link.group(1) if link else BASE + CARD_PATH.format(id=auc_id),
        })
    return rows, int(total.group(1)) if total else len(rows)


def parse_industries(page: str) -> dict[str, str]:
    """Страница выбора отраслей → идентификатор: название.

    Разметку этой страницы снять не удалось: она открывается отдельным окном и
    в сохранённых образцах её нет. Поэтому разбор нарочно нестрогий — берутся
    любые пары «значение — подпись». Пустой ответ не ошибка: вызывающий сам
    решает, что делать, и проход по отраслям просто пропускается.
    """
    found: dict[str, str] = {}
    for m in re.finditer(
            r'(?is)<input[^>]+value="(\d+)"[^>]*>\s*<label[^>]*>([^<]{3,120})</label>',
            page):
        found[m.group(1)] = " ".join(m.group(2).split())
    for m in re.finditer(
            r'(?is)value="(\d+)"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{3,120})', page):
        name = " ".join(m.group(2).split())
        if name:
            found.setdefault(m.group(1), name)
    return found


def parse_card(page: str, purchase_id: str) -> tuple[dict, list[dict], list[dict]]:
    """Карточка процедуры → строки для базы: закупка, её лоты, её файлы."""
    if "auctBlock" not in page or "af-request_end" not in page:
        raise SourceError(
            "разметка карточки не узнана (нет auctBlock или срока подачи). "
            "Возможно, площадка сменила шаблон или отдала страницу входа.")

    fields = {}
    for name, body in FIELD_RE.findall(page):
        value = AFV_RE.search(body)
        if value and name not in fields:
            fields[name] = value.group(1)
    text = {name: text_of(body) for name, body in fields.items()}

    # Заказчик приходит одним блоком: название, адрес, УНП — через <br>.
    customer = [line for line in text.get("customer_data", "").split("\n") if line]
    organizer = customer[0] if customer else None
    unp = next((line for line in reversed(customer) if line.isdigit()), None)
    location = "; ".join(customer[1:-1]) if len(customer) > 2 else None

    # Заголовков на странице два: «Просмотр закупки» в шапке блока и
    # «Процедура закупки № 2026-1349750» — нужен второй.
    number = None
    for head in H1_RE.findall(page):
        num = re.search(r"№\s*([\w-]+)", text_of(head))
        if num:
            number = num.group(1)
            break

    etp_url = None
    if "operator_site" in fields:
        link = HREF_RE.search(fields["operator_site"])
        etp_url = link.group(1) if link else None

    # Адрес для коммерческого предложения обычно спрятан в требованиях к
    # участникам, а не в графе контактов, поэтому почта собирается по всей карточке.
    emails = ", ".join(sorted(set(EMAIL_RE.findall(text_of(page)))))
    contacts = "\n".join(part for part in (text.get("customer_contacts"), emails) if part)

    form = FORM_RE.search(page)
    lots = parse_lots(page, purchase_id)
    purchase = {
        "id": purchase_id,
        "source": Icetrade.name,
        "number": number,
        "title": text.get("title", "").replace("\n", " ").strip(),
        # Своего поля состояния у карточки нет — оно проставлено у лотов.
        "state": next((lot["state"] for lot in lots if lot.get("state")), None),
        "tender_form": text_of(form.group(1)) if form else None,
        "organizer": organizer,
        "unp": unp,
        "location": location,
        "sum_lot": to_number(text.get("currency", "")),
        "created_ms": to_ms(text.get("created", "")),
        "updated_ms": None,
        "deadline_ms": to_ms(text.get("request_end", "")),
        "auction_url": etp_url,
        "page_url": BASE + CARD_PATH.format(id=purchase_id),
        "days_left": None,
        "industry": text.get("industry"),
        "contacts": contacts or None,
        "etp_url": etp_url,
    }
    return purchase, lots, parse_files(page, purchase_id)


def parse_lots(page: str, purchase_id: str) -> list[dict]:
    block = LOTS_RE.search(page)
    if not block:
        raise SourceError("в карточке нет таблицы лотов")
    body = block.group(1)

    # Подстроки лота: срок поставки, место, источник финансирования, код ОКРБ.
    # У первой из них впереди две служебные ячейки, поэтому берутся две последние.
    extras: dict[str, dict[str, str]] = {}
    for lot_no, sub in SUB_ROW_RE.findall(body):
        cells = CELL_RE.findall(sub)
        if len(cells) < 2:
            continue
        label = text_of(cells[-2][1]).rstrip(":")
        extras.setdefault(lot_no, {})[label] = text_of(cells[-1][1])

    lots = []
    for lot_no, row in LOT_ROW_RE.findall(body):
        cells = [cell for _, cell in CELL_RE.findall(row)]
        if len(cells) < 4:
            continue
        volume, unit, price = parse_amount(text_of(cells[2]))
        extra = extras.get(lot_no, {})
        okpb = next((v for k, v in extra.items() if "ОКРБ" in k), "")
        lots.append({
            "id": f"{purchase_id}-{lot_no}",
            "purchase_id": purchase_id,
            "lot_number": int(lot_no),
            "title": text_of(cells[1]).replace("\n", " ").strip(),
            "okpb": okpb,
            "volume": volume,
            "unit": unit,
            "price": price,
            "delivery": " ".join(extra.get("Срок поставки", "").split()),
            "state": text_of(cells[3]),
        })
    if not lots:
        raise SourceError("таблица лотов есть, но ни одной строки лота не разобрано")
    return lots


def parse_amount(cell: str) -> tuple[float | None, str | None, float | None]:
    """«1 компл., / 827 040 BYN» → количество, единица, сумма лота.

    Прочерк вместо суммы — обычное дело у запросов цен по постановлению № 168.
    Это не ошибка: лот идёт в список без оценки.
    """
    lines = [line for line in cell.split("\n") if line]
    volume = unit = price = None
    if lines:
        m = re.match(r"^([\d\s]*\d(?:[.,]\d+)?)\s*(.*?),?$", lines[0])
        if m:
            volume = to_number(m.group(1))
            unit = m.group(2).strip() or None
    for line in lines[1:]:
        if re.search(r"\d", line):
            price = to_number(line)
            break
    return volume, unit, price


def parse_files(page: str, purchase_id: str) -> list[dict]:
    """Приложенные документы. Часть процедур держит файлы на стороннем ЭТП."""
    files = []
    for cell in FILE_TD_RE.findall(page):
        for url, name in HREF_RE.findall(cell):
            files.append({
                "purchase_id": purchase_id,
                "idx": len(files),
                "name": text_of(name),
                "url": url if url.startswith("http") else BASE + url,
                "local": None,
                "status": "new",
            })
    return files
