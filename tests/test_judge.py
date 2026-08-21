"""Второй уровень отбора — тот, ради которого он затевался.

Разбор прогона 21.08.2026 показал, чего не берут правила: заказчик правильный
(БМЗ, Нафтан, Птицефабрика), а предмет — огнеупорная смесь, контакторы, пакеты,
счётчики воды. И наоборот: «поддон деревянный» и «поддон печи закалочной»
пишутся одинаково, а нужен только второй.

Сеть здесь не нужна: модель подменяется, проверяется вся обвязка — что уходит
в запрос, как разбирается ответ, что вердикт кэшируется по содержанию лота и
что отказ модели ничего не теряет.
"""
import json

from app import judge, store
from app.profile import load

PROFILE = load()

# Пары-ловушки: слева то, что оператор показал пальцем как лишнее.
TRAPS = [
    ("Поддон деревянный (паллета) 1200х800", "off"),
    ("Поддон печи закалочной по чертежу", "fit"),
    ("Форсунка ТНВД дизельного двигателя", "off"),
    ("Форсунка закалочной камеры печи", "fit"),
    ("Смеси огнеупорные для футеровки", "off"),
    ("Барабан волочильный согласно чертежу 420-М.714.080", "fit"),
]


def test_prompt_carries_capability_and_traps():
    """В запрос уходит признак производства и пары-ловушки, а не словарь."""
    system = judge.build_prompt(PROFILE, [])
    assert "чертеж" in system.lower()
    assert "поддон печи закалочной" in system.lower()
    assert "огнеупорн" in system.lower()


def test_prompt_carries_operator_examples():
    system = judge.build_prompt(
        PROFILE, [{"title": "Мембраны для клапана", "organizer": "ОАО «Нафтан»",
                   "verdict": "off"}])
    assert "Мембраны для клапана" in system
    assert "решения оператора" in system.lower()


def test_parse_answer_survives_a_talkative_model():
    """Модель охотно оборачивает JSON в ```-блок и предисловие."""
    text = ('Вот результат:\n```json\n'
            '[{"n": 1, "v": "off", "why": "огнеупор, не наш профиль"},'
            ' {"n": 2, "v": "fit", "why": "деталь по чертежу"}]\n```')
    out = judge.parse_answer(text, 2)
    assert [(i["n"], i["v"]) for i in out] == [(1, "off"), (2, "fit")]


def test_parse_answer_drops_garbage_but_keeps_the_rest():
    text = '[{"n": 1, "v": "неизвестно"}, {"n": 2, "v": "fit"}, {"n": 9, "v": "off"}]'
    out = judge.parse_answer(text, 2)
    assert [i["n"] for i in out] == [2]


def _db(tmp_path, monkeypatch, titles):
    monkeypatch.setattr(store, "DB", tmp_path / "t.db")
    conn = store.connect()
    conn.execute(
        "INSERT INTO purchases (id, source, organizer) VALUES ('p1', 'icetrade', 'ОАО «БМЗ»')")
    for n, title in enumerate(titles, 1):
        conn.execute(
            "INSERT INTO lots (id, purchase_id, lot_number, title, okpb, kind) "
            "VALUES (?, 'p1', ?, ?, '', 'supply')", (f"l{n}", n, title))
    conn.commit()
    return conn


def test_traps_are_sorted_by_the_model(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch, [t for t, _ in TRAPS])
    expected = {t: v for t, v in TRAPS}

    def fake_ask(cfg, system, batch):
        return [{"n": n, "v": expected[lot["title"]], "why": "тест"}
                for n, lot in enumerate(batch, 1)]

    monkeypatch.setattr(judge, "ask", fake_ask)
    cfg = {"openrouter_key": "k", "judge": True, "model": "m", "judge_batch": 20}
    stats = judge.review(conn, PROFILE, cfg)

    assert stats["judged"] == len(TRAPS)
    assert stats["off"] == 3
    got = {r["title"]: r["verdict"] for r in conn.execute("SELECT title, verdict FROM lots")}
    assert got == expected


def test_verdict_is_cached_by_content_not_by_lot_id(tmp_path, monkeypatch):
    """Повторная закупка той же детали не стоит ни одного запроса.

    Часть номенклатуры повторяется из тендера в тендер, и платить за неё
    второй раз незачем — ключ вешается на содержание, а не на номер лота.
    """
    conn = _db(tmp_path, monkeypatch, ["Смеси огнеупорные для футеровки"])
    calls = []

    def fake_ask(cfg, system, batch):
        calls.append(len(batch))
        return [{"n": 1, "v": "off", "why": "огнеупор"}]

    monkeypatch.setattr(judge, "ask", fake_ask)
    cfg = {"openrouter_key": "k", "judge": True, "model": "m", "judge_batch": 20}
    judge.review(conn, PROFILE, cfg)

    # тот же предмет пришёл под новым номером лота
    conn.execute("INSERT INTO lots (id, purchase_id, lot_number, title, okpb, kind) "
                 "VALUES ('l99', 'p1', 9, 'Смеси огнеупорные для футеровки', '', 'supply')")
    conn.commit()
    stats = judge.review(conn, PROFILE, cfg)

    assert calls == [1]                 # второй раз в модель не ходили
    assert stats["from_cache"] == 1
    assert conn.execute("SELECT verdict FROM lots WHERE id='l99'").fetchone()[0] == "off"


def test_operator_decision_outranks_the_model(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch, ["Поддон печи закалочной по чертежу"])
    key = store.lot_key("ОАО «БМЗ»", "Поддон печи закалочной по чертежу", "")
    store.save_verdict(conn, key, "fit", "решение оператора", "оператор")
    conn.commit()

    def fake_ask(cfg, system, batch):     # pragma: no cover — не должно вызываться
        raise AssertionError("оператор уже решил, спрашивать модель незачем")

    monkeypatch.setattr(judge, "ask", fake_ask)
    judge.review(conn, PROFILE,
                 {"openrouter_key": "k", "judge": True, "model": "m"})
    row = conn.execute("SELECT verdict, verdict_by FROM lots WHERE id='l1'").fetchone()
    assert row["verdict"] == "fit" and row["verdict_by"] == "оператор"

    # И модель эту правку не перебьёт даже при прямой записи.
    store.save_verdict(conn, key, "off", "передумала", "модель", "m")
    assert conn.execute("SELECT verdict FROM verdicts WHERE key=?",
                        (key,)).fetchone()[0] == "fit"


def test_without_a_key_nothing_is_lost(tmp_path, monkeypatch):
    """Ключа нет — сбор доходит до конца, лоты помечены «не проверено»."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    conn = _db(tmp_path, monkeypatch, ["Барабан волочильный по чертежу"])
    stats = judge.review(conn, PROFILE, {"openrouter_key": "", "judge": True})
    row = conn.execute("SELECT verdict, verdict_why FROM lots WHERE id='l1'").fetchone()
    assert stats["judged"] == 0
    assert row["verdict"] == "maybe" and "не проверено" in row["verdict_why"]


def test_model_failure_does_not_drop_lots(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch, ["Барабан волочильный по чертежу"])

    def fake_ask(cfg, system, batch):
        raise RuntimeError("OpenRouter ответил 429: rate limit")

    monkeypatch.setattr(judge, "ask", fake_ask)
    stats = judge.review(conn, PROFILE,
                         {"openrouter_key": "k", "judge": True, "model": "m"})
    assert any("429" in e for e in stats["errors"])
    assert conn.execute("SELECT verdict FROM lots WHERE id='l1'").fetchone()[0] == "maybe"


def test_key_never_appears_in_logs():
    masked = judge.mask_key("sk-or-v1-0123456789abcdef0123456789abcdef")
    assert "0123456789abcdef" not in masked
    assert masked.startswith("sk-or-v")


def test_free_models_filters_by_price(monkeypatch):
    payload = {"data": [
        {"id": "vendor/free:free", "name": "Free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "vendor/paid", "name": "Paid", "pricing": {"prompt": "0.000001", "completion": "0"}},
    ]}

    class Answer:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload

    monkeypatch.setattr(judge.requests, "get", lambda *a, **k: Answer())
    assert [m["id"] for m in judge.free_models({})] == ["vendor/free:free"]


def test_batches_are_split(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch, [f"Деталь {n} по чертежу" for n in range(5)])
    sizes = []

    def fake_ask(cfg, system, batch):
        sizes.append(len(batch))
        return [{"n": n, "v": "fit", "why": ""} for n in range(1, len(batch) + 1)]

    monkeypatch.setattr(judge, "ask", fake_ask)
    judge.review(conn, PROFILE,
                 {"openrouter_key": "k", "judge": True, "model": "m", "judge_batch": 2})
    assert sizes == [2, 2, 1]


def test_request_lists_lot_purchase_and_files(monkeypatch):
    """В запрос уходит контекст лота, а не одно название."""
    seen = {}

    class Answer:
        status_code = 200
        def json(self):
            seen["body"] = json.loads(seen["raw"])
            return {"choices": [{"message": {"content": '[{"n":1,"v":"fit"}]'}}]}

    def fake_post(url, json=None, headers=None, **kw):
        seen["raw"] = __import__("json").dumps(json)
        seen["headers"] = headers
        return Answer()

    monkeypatch.setattr(judge.requests, "post", fake_post)
    judge.ask({"model": "m", "openrouter_key": "k"}, "система", [{
        "title": "Барабан тянущий", "purchase_title": "Аукцион",
        "okpb": "25.99.29.400", "organizer": "ОАО «БМЗ»",
        "files": ["baraban-420-m.714.080.pdf"]}])
    user = seen["body"]["messages"][1]["content"]
    assert "Барабан тянущий" in user and "25.99.29.400" in user
    assert "baraban-420" in user
    assert seen["headers"]["Authorization"] == "Bearer k"


def test_operator_marks_become_examples_for_the_model(tmp_path, monkeypatch):
    """Пометка «мимо профиля» руками — это и есть отработка.

    Каждое решение оператора уходит в следующий запрос как пример, поэтому
    список чистится сам, а не только руками.
    """
    conn = _db(tmp_path, monkeypatch, ["Кокцидиостатик гранулированный"])
    key = store.lot_key("ОАО «БМЗ»", "Кокцидиостатик гранулированный", "")
    store.save_verdict(conn, key, "off", "решение оператора", "оператор", "",
                       "Кокцидиостатик гранулированный", "ОАО «БМЗ»")
    conn.commit()

    system = judge.build_prompt(PROFILE, store.examples(conn))
    assert "Кокцидиостатик" in system


def test_key_is_read_from_env_when_settings_are_empty(monkeypatch):
    """Ключ кладут в .env — там его правят в редакторе, не запуская приложение."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-from-env")
    assert judge.api_key({"openrouter_key": ""}) == "sk-or-v1-from-env"
    assert judge.key_source({"openrouter_key": ""}) == ".env"
    # Вписанное руками в настройках сильнее — иначе оно молча не работало бы.
    assert judge.api_key({"openrouter_key": "sk-or-v1-typed"}) == "sk-or-v1-typed"
    assert judge.key_source({"openrouter_key": "sk-or-v1-typed"}) == "настройки"


def test_env_file_is_parsed(tmp_path, monkeypatch):
    from app import settings as settings_mod
    monkeypatch.setattr(settings_mod, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        '# комментарий\nOPENROUTER_API_KEY="sk-or-v1-quoted"\nПУСТО=\n',
        encoding="utf-8")
    settings_mod.load_env()
    assert judge.api_key({}) == "sk-or-v1-quoted"


def test_env_file_is_created_if_missing(tmp_path, monkeypatch):
    """Чтобы ключ было куда вписать: пустой .env заводится сам."""
    from app import settings as settings_mod
    target = tmp_path / ".env"
    monkeypatch.setattr(settings_mod, "ENV_FILE", target)
    settings_mod.load_env()
    assert target.exists() and "OPENROUTER_API_KEY=" in target.read_text(encoding="utf-8")
