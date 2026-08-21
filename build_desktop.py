"""Сборка десктопной версии: один .exe, запускается двойным щелчком.

    .venv/Scripts/python.exe build_desktop.py

Результат — `dist/Tender-PTS/Tender-PTS.exe`. Рядом с ним при первом запуске
появятся `profile.json`, `.env` и папка `work/`: их правят руками, поэтому они
лежат снаружи сборки, а не внутри.

Почему папка, а не одиночный файл: onefile каждый раз распаковывает себя во
временный каталог — это лишние секунды на старте и лишние сюрпризы с путями.
Папку проще положить на сетевой диск и передать целиком.

Ключ OpenRouter в сборку **не попадает**: `.env` и `work/` исключены нарочно,
чтобы собранное приложение можно было отдать кому угодно.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Tender-PTS"

# uvicorn и pywebview находят своё через importlib, поэтому PyInstaller их
# зависимости сам не видит — перечисляем явно.
HIDDEN = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "webview.platforms.edgechromium", "webview.platforms.winforms",
]


def main() -> int:
    for folder in ("build", "dist"):
        shutil.rmtree(ROOT / folder, ignore_errors=True)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", NAME,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        # Интерфейс — обычные файлы, их надо положить в сборку как есть.
        "--add-data", f"{ROOT / 'app' / 'web'}{os.pathsep}app/web",
        # Профиль вшивается как образец: наружу он выкладывается при первом запуске.
        "--add-data", f"{ROOT / 'profile.json'}{os.pathsep}.",
    ]
    for module in HIDDEN:
        command += ["--hidden-import", module]
    command.append(str(ROOT / "desktop.py"))

    print("собираю…", " ".join(command[:6]))
    result = subprocess.run(command)
    if result.returncode:
        return result.returncode

    exe = ROOT / "dist" / NAME / f"{NAME}.exe"
    print("готово:", exe if exe.exists() else "файл не найден — смотрите вывод выше")
    return 0 if exe.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
