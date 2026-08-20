"""Проверки отбора по профилю.

Главный случай — из разведки: самые крупные лоты Амкодора лежат в закупках с
названием «Аукцион» или «Закупка из одного источника», а деталь видна только
в названии лота или в имени приложенного файла.
"""
from app import matching
from app.profile import load, translit

PROFILE = load()


def test_translit_matches_platform_scheme():
    """Схема снята с реальных имён файлов goszakupki.by."""
    assert translit("шестерня") == "shesternya"
    assert translit("фланец") == "flanec"
    assert translit("поршень") == "porshen"
    assert translit("стакан") == "stakan"


def test_lot_title_wins_over_generic_purchase_title():
    m = matching.match_lot(
        PROFILE, 'Объемная штамповка из стали "Вал шестерня" №97', "Аукцион")
    assert m.matched and m.kind == "supply"


def test_matched_by_attached_file_name():
    """Лот назван невнятно, деталь видна только по имени вложения.

    Имя файла настоящее, с goszakupki.by.
    """
    m = matching.match_lot(PROFILE, "Позиция 1", "Закупка из одного источника",
                           filenames=["val-shesternya-97_1786700843.jpg",
                                      "dogovor-postavki.pdf"])
    assert m.matched
    assert m.reason == "имя приложенного файла"


def test_partial_translit_does_not_match():
    """Файл с одной шестернёй не должен срабатывать на ключ «вал шестерня».

    Короткие основы («val») ищутся только как отдельное слово, иначе составной
    ключ вырождался бы в одиночный и ловил лишнее.
    """
    m = matching.match_lot(PROFILE, "Позиция 1", "Закупка",
                           filenames=["shesternya-56_1786700835.jpg"])
    assert not m.matched


def test_short_translit_does_not_match_random_file():
    m = matching.match_lot(PROFILE, "Позиция 1", "Закупка",
                           filenames=["valenki-katanye.pdf"])
    assert not m.matched


def test_service_is_separated_from_supply():
    m = matching.match_lot(PROFILE, "Замена теплообменника в жилом доме №44", "")
    assert m.matched and m.kind == "service"


def test_supply_marker_beats_service_marker():
    m = matching.match_lot(PROFILE, "Поставка и монтаж теплообменника", "")
    assert m.kind == "supply"


def test_stop_word_filters_medical_homonym():
    m = matching.match_lot(
        PROFILE, "Теплообменник к аппарату искусственного кровообращения", "")
    assert m.kind == "stop"


def test_watched_organizer_recognised():
    assert matching.match_by_organizer(PROFILE, 'ОАО "Кузлитмаш"') is not None
    assert matching.match_by_organizer(PROFILE, 'ОАО "Амкодор-Унимод"') is not None
    assert matching.match_by_organizer(PROFILE, 'ГУ "Школа №5"') is None
