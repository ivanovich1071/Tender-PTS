#!/usr/bin/env python3
"""Разведка icetrade.by: снять реальную структуру страниц.

Площадка закрыта для адресов вне Беларуси (403 на любой путь, включая
robots.txt), поэтому написать разбор вслепую нельзя — гадать по неизвестной
разметке бессмысленно. Этот скрипт запускается с машины, у которой доступ есть,
и сохраняет образцы страниц. По ним настраивается `app/sources/icetrade.py`.

    python recon/icetrade_probe.py
    python recon/icetrade_probe.py --proxy socks5://user:pass@host:port

Что делает:
  1. проверяет доступ к главной;
  2. пробует набор возможных путей поиска и показывает, какие ответили;
  3. сохраняет HTML в recon/out/icetrade/ и печатает подсказки по разметке —
     формы, таблицы, ссылки на процедуры, признаки JSON API.

Результат нужен целиком: пришлите содержимое recon/out/icetrade/STRUCTURE.md
и сами html-файлы.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

import requests
import urllib3

OUT = Path(__file__).parent / "out" / "icetrade"
BASE = "https://icetrade.by"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Пути наугад: какой ответит, тот и настоящий.
CANDIDATE_PATHS = [
    "/", "/robots.txt", "/sitemap.xml",
    "/search", "/search/", "/search/auctions", "/search/auction",
    "/auctions", "/tenders", "/tender", "/purchases",
    "/ru/search", "/ru/auctions",
    "/api", "/api/v1", "/api/search",
    "/search?search_text=%D1%88%D0%B5%D1%81%D1%82%D0%B5%D1%80%D0%BD%D1%8F",
    "/search/auctions?search_text=%D1%88%D0%B5%D1%81%D1%82%D0%B5%D1%80%D0%BD%D1%8F",
]


def make_session(proxy: str | None) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def fetch(s: requests.Session, url: str, timeout: int = 25):
    """Возвращает (статус, текст, заметка). Битую цепочку TLS обходим повтором."""
    try:
        r = s.get(url, timeout=timeout)
        return r.status_code, r.text, ""
    except requests.exceptions.SSLError:
        urllib3.disable_warnings()
        try:
            r = s.get(url, timeout=timeout, verify=False)
            return r.status_code, r.text, "TLS без проверки (нет промежуточного сертификата)"
        except Exception as e:
            return None, "", f"{type(e).__name__}: {e}"
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}"


def describe(html: str) -> dict:
    """Подсказки по разметке: за что цепляться разбору."""
    links = re.findall(r'href=["\']([^"\']+)["\']', html)
    numeric = [l for l in links if re.search(r"/\d{4,}", l)]
    patterns = Counter(re.sub(r"\d+", "{id}", l.split("?")[0]) for l in numeric)
    return {
        "size": len(html),
        "title": (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) or
                  [None, ""])[1].strip()[:120],
        "forms": re.findall(r'<form[^>]*action=["\']([^"\']*)["\']', html)[:10],
        "inputs": re.findall(r'<input[^>]*name=["\']([^"\']+)["\']', html)[:20],
        "tables": html.count("<table"),
        "link_patterns": patterns.most_common(12),
        "looks_like_json_api": bool(re.search(r"/api/|fetch\(|XMLHttpRequest", html)),
        "script_urls": re.findall(r'<script[^>]*src=["\']([^"\']+)["\']', html)[:10],
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Разведка структуры icetrade.by")
    ap.add_argument("--proxy", default="", help="socks5://user:pass@host:port")
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--timeout", type=int, default=12, help="таймаут запроса, сек")
    args = ap.parse_args()

    if args.proxy and ("user:pass" in args.proxy or "host:port" in args.proxy):
        print("В --proxy остался образец из инструкции. Подставьте настоящие "
              "адрес, порт и, если нужны, логин с паролем.")
        return 2

    if args.proxy.startswith("socks"):
        try:
            import socks  # noqa: F401
        except ImportError:
            print("Для socks-прокси нужен PySocks:")
            print('    .venv\\Scripts\\python.exe -m pip install "requests[socks]"')
            return 2

    OUT.mkdir(parents=True, exist_ok=True)
    s = make_session(args.proxy or None)

    print(f"icetrade · {args.base}")
    print("прокси:", args.proxy.split("@")[-1] if args.proxy else "не задан")
    print()

    report: list[str] = ["# icetrade.by — снимок структуры", ""]
    if args.proxy:
        report.append(f"Прокси: `{args.proxy.split('@')[-1]}`")
    report += ["", "| путь | ответ | размер | заметка |", "|---|---|---|---|"]

    saved = 0
    dead = 0
    for path in CANDIDATE_PATHS:
        url = urljoin(args.base, path)
        code, text, note = fetch(s, url, timeout=args.timeout)
        size = len(text or "")
        mark = "—" if code is None else str(code)
        note = note.split("(Caused by")[0].strip()[:90]
        print(f"  {mark:>4}  {size:>8}  {path}  {note}")
        report.append(f"| `{path}` | {mark} | {size} | {note} |")

        # Если сервер не отвечает вовсе, перебирать остальные пути незачем:
        # каждый стоит полного таймаута, а ответ уже понятен.
        dead = dead + 1 if code is None else 0
        if dead >= 3:
            print("\n  сервер не отвечает — остальные пути не проверяю")
            report.append("| … | — | — | сервер не отвечает, перебор прекращён |")
            break

        if code == 200 and size > 500:
            name = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") or "root"
            (OUT / f"{name}.html").write_text(text, encoding="utf-8")
            saved += 1

    print()
    report += ["", "## Разметка отвечающих страниц", ""]
    for file in sorted(OUT.glob("*.html")):
        info = describe(file.read_text(encoding="utf-8", errors="ignore"))
        print(f"— {file.name}: {info['title']!r}, таблиц {info['tables']}, "
              f"шаблоны ссылок {info['link_patterns'][:3]}")
        report += [f"### {file.name}", "```json",
                   json.dumps(info, ensure_ascii=False, indent=1), "```", ""]

    (OUT / "STRUCTURE.md").write_text("\n".join(report), encoding="utf-8")
    print()
    print(f"сохранено страниц: {saved}")
    print(f"отчёт: {OUT / 'STRUCTURE.md'}")
    if not saved:
        print()
        print("Ни одна страница не открылась. Если везде 403 — прокси не белорусский "
              "или не применился; проверьте его отдельно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
