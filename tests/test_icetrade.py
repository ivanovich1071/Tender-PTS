"""Разбор страниц icetrade — на настоящих страницах, без сети.

Фикстуры в tests/fixtures/icetrade — это публичные страницы процедур и выдачи
поиска, снятые с площадки 20.08.2026. Они же служат договором: если icetrade
сменит шаблон, тесты упадут здесь, а не в тишине во время сбора.

Две карточки выбраны нарочно разными:

* 1349750 — открытый конкурс Гомельского химического завода, цена есть,
  файлы лежат на самом icetrade;
* 1364146 — заявка о ценах БМЗ по постановлению № 168, цены нет вовсе,
  а документация лежит на стороннем ЭТП goszakupki.by.
"""
from pathlib import Path

import pytest

from app.sources import icetrade
from app.sources.base import SourceError

FIXTURES = Path(__file__).parent / "fixtures" / "icetrade"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def konkurs():
    return icetrade.parse_card(read("card_1349750.html"), "1349750")


@pytest.fixture(scope="module")
def zayavka():
    return icetrade.parse_card(read("card_1364146.html"), "1364146")


# --- выдача поиска -------------------------------------------------------

def test_results_read_every_column():
    rows, total = icetrade.parse_results(read("search_shesternya.html"))
    assert total == 4 and len(rows) == 4
    first = rows[0]
    assert first["id"] == "1364218"
    assert first["number"] == "2026-1364218"
    assert first["organizer"] == 'Государственное предприятие "Шахтострой"'
    assert first["price"] == pytest.approx(18760.80)
    assert first["page_url"] == "https://icetrade.by/tenders/all/view/1364218"


def test_results_carry_deadline_so_cards_are_not_fetched_in_vain():
    rows, _ = icetrade.parse_results(read("search_shesternya.html"))
    assert all(row["deadline_ms"] for row in rows)


def test_results_refuse_a_page_that_is_not_the_listing():
    with pytest.raises(SourceError):
        icetrade.parse_results("<html><body>что-то другое</body></html>")


# --- карточка ------------------------------------------------------------

def test_card_reads_the_head(konkurs):
    purchase, _, _ = konkurs
    assert purchase["number"] == "2026-1349750"
    assert purchase["tender_form"] == "Открытый конкурс"
    assert purchase["organizer"] == 'Открытое акционерное общество "Гомельский химический завод"'
    assert purchase["unp"] == "400069905"
    assert purchase["sum_lot"] == pytest.approx(827040.0)
    assert purchase["industry"] == "Машиностроение > Другое"
    assert purchase["source"] == "icetrade"


def test_card_takes_the_second_heading_not_the_first(konkurs):
    """На странице два h1: «Просмотр закупки» и номер процедуры. Нужен второй."""
    purchase, _, _ = konkurs
    assert purchase["number"] and purchase["number"] != "Просмотр закупки"


def test_lot_has_quantity_price_and_okrb(konkurs):
    _, lots, _ = konkurs
    assert len(lots) == 1
    lot = lots[0]
    assert lot["volume"] == pytest.approx(1.0)
    assert lot["unit"] == "компл."
    assert lot["price"] == pytest.approx(827040.0)
    assert lot["okpb"] == "28.13.12"          # из подстроки лота, а не из шапки
    assert lot["delivery"] == "c 01.01.2027 по 30.03.2027"
    assert lot["state"] == "Подача предложений"


def test_files_are_open_links_without_login(konkurs):
    _, _, files = konkurs
    assert len(files) == 4
    assert files[0]["url"] == (
        "https://icetrade.by/auction/getFile/auction/1349750?f=detail&n=1")
    assert files[0]["name"].endswith(".doc")


# --- заявка о ценах: цены нет, файлы на чужом ЭТП -------------------------

def test_missing_price_is_empty_not_zero(zayavka):
    """Площадка пишет «0 BYN», когда цена не объявлена. Ноль обманывает оператора."""
    purchase, lots, _ = zayavka
    assert purchase["sum_lot"] is None
    assert lots[0]["price"] is None
    assert lots[0]["volume"] == pytest.approx(2.0)


def test_documents_may_live_on_another_platform(zayavka):
    purchase, _, files = zayavka
    assert purchase["etp_url"] == "https://goszakupki.by/marketing/view/3592147"
    assert len(files) == 2
    assert all(f["url"].startswith("https://goszakupki.by/") for f in files)


def test_contacts_collect_the_address_for_a_quotation(zayavka):
    """Адрес для КП спрятан в требованиях к участникам, а не в графе контактов."""
    purchase, _, _ = zayavka
    assert "rdc.uko@bmz.gomel.by" in purchase["contacts"]


# --- отказ вместо выдумки ------------------------------------------------

def test_card_refuses_a_login_page():
    with pytest.raises(SourceError, match="разметка карточки"):
        icetrade.parse_card("<html><body>Вход для пользователей</body></html>", "1")


def test_card_refuses_when_lots_table_disappears():
    broken = read("card_1349750.html").replace('id="lots_list"', 'id="something_else"')
    with pytest.raises(SourceError, match="таблиц"):
        icetrade.parse_card(broken, "1349750")


# --- адрес поиска --------------------------------------------------------

def test_search_url_filters_by_deadline_not_by_publication_date():
    """Закупка химзавода размещена 25.06, а принимает предложения до 31.08:
    окно по дате размещения её бы потеряло."""
    url = icetrade.Icetrade({}).search_url(search_text="шестерня")
    assert "request_end_from=" in url and "created_from=&" in url
    assert "search=%D0%9D%D0%B0%D0%B9%D1%82%D0%B8" in url   # без «Найти» выдачи нет
    assert "t%5BMarketingForPrice%5D=1" in url              # запросы цен тоже нужны
