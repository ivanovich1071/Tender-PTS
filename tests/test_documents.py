"""Скачивание документации: файл — это файл, а не страница площадки.

Случай настоящий. 21.08.2026 у двух закупок «скачалось» по четыре документа,
все ровно по 17,5 КБ, и внутри каждого — главная страница icetrade. Так
отвечает закрытая по региону площадка: HTTP 200 и своя главная вместо
документа. Проверялся только код ответа, поэтому оператор видел «скачан» и
«открыть», а браузер — «Не удалось загрузить PDF-документ».

Страница из того прогона лежит в tests/fixtures/icetrade/blocked_page.html.
"""
from pathlib import Path

from app import documents

FIXTURES = Path(__file__).parent / "fixtures" / "icetrade"
BLOCKED = (FIXTURES / "blocked_page.html").read_bytes()


def test_platform_page_is_not_a_document():
    refusal = documents.page_instead_of_file(
        "text/html; charset=utf-8", BLOCKED, "scan20260615.pdf")
    assert refusal
    assert "icetrade" in refusal.lower()       # видно, чья это страница


def test_page_is_caught_even_without_content_type():
    """Заголовку верить нельзя — площадка отдавала HTML под видом файла."""
    assert documents.page_instead_of_file("application/pdf", BLOCKED, "tz.pdf")


def test_real_documents_pass():
    assert documents.page_instead_of_file("application/pdf", b"%PDF-1.7\n%...", "tz.pdf") == ""
    assert documents.page_instead_of_file("", b"PK\x03\x04\x14\x00", "tz.docx") == ""
    assert documents.page_instead_of_file(
        "", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "kd.doc") == ""


def test_html_file_is_allowed_when_it_is_asked_for():
    assert documents.page_instead_of_file("text/html", BLOCKED, "izveschenie.html") == ""


def test_saved_stub_is_recognised_on_disk(tmp_path):
    good = tmp_path / "chertezh.pdf"
    good.write_bytes(b"%PDF-1.4\nsomething")
    stub = tmp_path / "tz.pdf"
    stub.write_bytes(BLOCKED)
    assert documents.looks_broken(stub)
    assert not documents.looks_broken(good)
