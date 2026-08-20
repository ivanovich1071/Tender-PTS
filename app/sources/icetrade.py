"""Источник: icetrade.by — закупки за счёт собственных средств и в строительстве.

Почему это важнее ГИАС для ПТС. ГИАС — бюджетная сфера. Крупные заводы
(БМЗ, БЕЛАЗ, ГродноАзот, Беларуськалий, Гомельский химический) закупают за
собственные средства, и это идёт сюда. Проверено 20.08.2026: у БМЗ в ГИАС
100 закупок за шесть лет, и то тепловизоры и дизтопливо, у Гомельского
химического последняя закупка в ГИАС — за 2023 год.

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

**Структура страниц не подтверждена.** Разметку снять неоткуда: площадка
закрыта для всех адресов, с которых писался этот код. Пути и селекторы лежат
в settings → icetrade и правятся без изменения кода, а снять реальную
структуру помогает `recon/icetrade_probe.py` — он запускается с машины,
у которой есть доступ, и сохраняет образцы страниц.
"""
from __future__ import annotations

import re

import requests

from app.sources.base import NotConfigured, SourceError

BASE = "https://icetrade.by"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Значения по умолчанию для settings → icetrade. Пути предположительные:
# подтвердить и поправить по результату recon/icetrade_probe.py.
DEFAULTS = {
    "enabled": False,
    "base": BASE,
    "search_path": "/search/auctions?search_text={query}",
    "card_path": "/auction/{id}",
    "allow_insecure_tls": True,
    "structure_confirmed": False,
}


class Icetrade:
    name = "icetrade"
    title = "icetrade.by — закупки за собственные средства"

    def __init__(self, cfg: dict):
        self.cfg = {**DEFAULTS, **(cfg.get("icetrade") or {})}
        self.proxy = (cfg.get("proxy") or "").strip()
        self.warnings: list[str] = []
        self.calls = 0
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        if self.proxy:
            self.s.proxies = {"http": self.proxy, "https": self.proxy}

    # --- транспорт -------------------------------------------------------

    def get(self, url: str, timeout: int = 30) -> str:
        """GET с одной уступкой: повтор без проверки TLS при битой цепочке."""
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
            html = self.get(self.cfg["base"] + "/", timeout=25)
        except SourceError as e:
            return {"ok": False, "reason": str(e), "proxy": bool(self.proxy)}
        return {"ok": True, "size": len(html), "proxy": bool(self.proxy),
                "warnings": self.warnings}

    # --- сбор ------------------------------------------------------------

    def harvest(self, profile, cfg: dict, progress=None, cancelled=None):
        if not self.cfg.get("structure_confirmed"):
            raise NotConfigured(
                "icetrade.by: разметка страниц не подтверждена. Запустите "
                "`python recon/icetrade_probe.py --proxy <ваш прокси>` с машины, "
                "у которой есть доступ, — он сохранит образцы страниц, по ним "
                "настраиваются пути и разбор. Пока источник выключен.")

        def say(msg: str) -> None:
            if progress:
                progress(f"icetrade · {msg}")

        queries = [text for text, _ in profile.queries()]
        queries += [o["query"] for o in profile.organizers]
        for i, query in enumerate(queries, 1):
            if cancelled and cancelled():
                return
            say(f"поиск {i}/{len(queries)}: {query}")
            url = self.cfg["base"] + self.cfg["search_path"].format(
                query=requests.utils.quote(query))
            html = self.get(url)
            for item in parse_search(html):
                yield from ()   # разбор карточек включается после подтверждения разметки
                del item


def parse_search(html: str) -> list[dict]:
    """Ссылки на процедуры из страницы поиска.

    Пока это заглушка на регулярном выражении: подтверждённой разметки нет.
    После прогона `recon/icetrade_probe.py` заменяется разбором по реальной
    структуре.
    """
    ids = set(re.findall(r"/auction/(\d+)", html))
    return [{"id": i} for i in sorted(ids)]
