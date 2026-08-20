#!/usr/bin/env python3
"""Проверка прокси перед разведкой площадок.

Отдельный скрипт, потому что «icetrade не открылся» и «прокси не работает» —
разные беды с одинаковым видом. Здесь по шагам: разобрана ли строка, отвечает
ли прокси, из какой страны виден выход, и открываются ли белорусские площадки.

    python recon/proxy_check.py --proxy socks5://логин:пароль@1.2.3.4:1080
    python recon/proxy_check.py                     # проверить без прокси

Схема прокси: socks5:// для socks-прокси, http:// для обычного.
Если прокси без авторизации — логин с паролем не нужны:
    socks5://1.2.3.4:1080
"""
from __future__ import annotations

import argparse
import re
import sys
from urllib.parse import urlparse

import requests
import urllib3

# Хосты, ради которых всё затевается.
TARGETS = [
    ("gias.by", "https://gias.by/", "ГИАС — работает и без прокси"),
    ("goszakupki.by", "https://goszakupki.by/", "файлы документации"),
    ("icetrade.by", "https://icetrade.by/", "закупки за собственные средства"),
    ("zakupki.butb.by", "https://zakupki.butb.by/", "БУТБ"),
]

PLACEHOLDERS = ["логин", "пароль", "адрес", "порт", "user", "pass",
                "host", "port", "хост", "ваш"]


def validate(proxy: str) -> str:
    """Понятная жалоба вместо стека урллиба."""
    if any(p in proxy.lower() for p in PLACEHOLDERS):
        return ("в строке остались слова-заполнители из инструкции. Их нужно "
                "заменить настоящими значениями прокси, например:\n"
                "    socks5://ivan:s3cret@178.172.10.20:1080\n"
                "    socks5://178.172.10.20:1080      (если прокси без пароля)")
    if "://" not in proxy:
        return "нет схемы. Начните строку с socks5:// или http://"
    parsed = urlparse(proxy)
    if parsed.scheme not in ("socks5", "socks5h", "socks4", "http", "https"):
        return f"схема {parsed.scheme!r} не поддерживается, нужна socks5:// или http://"
    if not parsed.hostname:
        return "не разобран адрес прокси"
    if not parsed.port:
        return "не указан порт. Он идёт через двоеточие после адреса: ...:1080"
    if re.search(r"[а-яё]", proxy, re.I):
        return "в строке есть кириллица — адрес, логин и пароль латиницей"
    return ""


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Проверка прокси и доступа к площадкам")
    ap.add_argument("--proxy", default="", help="socks5://логин:пароль@адрес:порт")
    ap.add_argument("--timeout", type=int, default=15)
    args = ap.parse_args()

    proxies = None
    if args.proxy:
        problem = validate(args.proxy)
        if problem:
            print("Прокси задан неверно:", problem)
            return 2
        proxies = {"http": args.proxy, "https": args.proxy}
        if args.proxy.startswith("socks"):
            try:
                import socks  # noqa: F401
            except ImportError:
                print("Для socks-прокси нужен PySocks:")
                print('    .venv\\Scripts\\python.exe -m pip install "requests[socks]"')
                return 2
        host = urlparse(args.proxy).hostname
        print(f"Прокси: {host}:{urlparse(args.proxy).port}")
    else:
        print("Прокси не задан — проверяю прямое соединение.")
    print()

    # 1. Откуда мы видны интернету
    print("1. Внешний адрес")
    try:
        r = requests.get("https://ipinfo.io/json", proxies=proxies,
                         timeout=args.timeout)
        info = r.json()
        country = info.get("country", "?")
        print(f"   {info.get('ip')}  страна {country}  {info.get('org', '')[:50]}")
        if args.proxy and country != "BY":
            print("   Это не Беларусь. icetrade и goszakupki будут закрыты — "
                  "нужен прокси именно с белорусским адресом.")
        elif country == "BY":
            print("   Адрес белорусский — то, что нужно.")
    except Exception as e:
        print(f"   не удалось: {type(e).__name__}: {str(e)[:110]}")
        if args.proxy:
            print("   Прокси не отвечает. Проверьте адрес, порт, логин и пароль, "
                  "и не истёк ли срок аренды.")
        return 1
    print()

    # 2. Сами площадки
    print("2. Площадки")
    urllib3.disable_warnings()
    for host, url, why in TARGETS:
        try:
            r = requests.get(url, proxies=proxies, timeout=args.timeout,
                             verify=False, headers={"User-Agent": "Mozilla/5.0"})
            mark = "ок " if r.status_code == 200 else f"{r.status_code}"
            note = "" if r.status_code == 200 else " — закрыт для этого адреса"
            print(f"   {mark:>4}  {host:18} {why}{note}")
        except Exception as e:
            print(f"   —     {host:18} {why} — {type(e).__name__}")
    print()
    print("Дальше: если icetrade ответил 200, запускайте разведку структуры")
    print("    .venv\\Scripts\\python.exe recon\\icetrade_probe.py --proxy <та же строка>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
