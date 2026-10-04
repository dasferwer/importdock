from decimal import Decimal

import pytest
from openpyxl import Workbook

from importdock.parser import columns, rows, validate


def test_columns_and_missing_field():
    assert columns(["Цена", "Код"], {"external_id": "Код", "amount": "Цена"}) == {
        "external_id": 1,
        "amount": 0,
    }
    for header, mapping in [
        (["a", "a"], {"external_id": "a", "amount": "a"}),
        (["a"], {"external_id": "a"}),
        (["a", "b"], {"external_id": "a", "amount": "c"}),
    ]:
        with pytest.raises(ValueError):
            columns(header, mapping)


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "1.001", "10000000000000000", "=2+2", "x"])
def test_invalid_amount(amount):
    with pytest.raises(ValueError):
        validate(["id", amount], {"external_id": 0, "amount": 1})


def test_correct_amount():
    assert validate([" id ", "-12.50"], {"external_id": 0, "amount": 1}) == (
        "id",
        Decimal("-12.50"),
    )


def test_csv_quoted_and_xlsx_formula(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text('id,amount\n"a,b",12.30\n')
    assert list(rows(path, "csv"))[1] == ["a,b", "12.30"]
    book = Workbook()
    book.active.append(["id", "amount"])
    book.active.append(["one", "=1+1"])
    path = tmp_path / "data.xlsx"
    book.save(path)
    assert list(rows(path, "xlsx"))[1] == ["one", "=1+1"]


def test_malformed_multiline_csv_is_not_silently_accepted(tmp_path):
    from importdock.parser import SeekableCSV

    path = tmp_path / "broken.csv"
    path.write_text('id,amount\n"незакрытое поле,1\n')
    stream = SeekableCSV(path)
    try:
        assert next(stream) == ["id", "amount"]
        with pytest.raises(ValueError, match="Повреждён CSV"):
            next(stream)
    finally:
        stream.close()


@pytest.mark.parametrize(
    "payload",
    [
        b'id,amount\n"open,1\n',
        b'id,amount\n"a"broken,1\n',
        b'id,amount\n"' + b"x" * (1024 * 1024) + b'",1\n',
        b'id,amount\n"' + b"x\n" * (1024 * 1024) + b'",1\n',
        b"id,amount\n\xff,1\n",
    ],
    ids=["unclosed", "broken-quote", "physical-limit", "logical-limit", "utf8"],
)
def test_preview_and_worker_share_strict_csv_failures(tmp_path, payload):
    from importdock.parser import SeekableCSV

    path = tmp_path / "bad.csv"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        list(rows(path, "csv"))
    stream = SeekableCSV(path)
    try:
        with pytest.raises(ValueError):
            list(stream)
    finally:
        stream.close()


@pytest.mark.parametrize("mode", ["eof", "early-close", "error"])
def test_preview_closes_underlying_csv_stream(tmp_path, monkeypatch, mode):
    import builtins

    from importdock import parser

    path = tmp_path / "input.csv"
    path.write_bytes(b'id,amount\n"unclosed,1\n' if mode == "error" else b"id,amount\na,1\n")
    opened = []

    def track(*args, **kwargs):
        stream = builtins.open(*args, **kwargs)  # noqa: SIM115 — поток передаётся парсеру.
        opened.append(stream)
        return stream

    monkeypatch.setattr(parser, "open", track, raising=False)
    iterator = rows(path, "csv")
    assert next(iterator) == ["id", "amount"]
    if mode == "error":
        with pytest.raises(ValueError):
            list(iterator)
    elif mode == "eof":
        assert list(iterator) == [["a", "1"]]
    else:
        iterator.close()
    assert len(opened) == 1 and opened[0].closed


def test_seekable_csv_offset_follows_bom_multiline_crlf_record(tmp_path):
    from importdock.parser import SeekableCSV

    path = tmp_path / "multi.csv"
    header = b"\xef\xbb\xbfid,amount\r\n"
    first = '"Москва\r\nулица 5",1.25\r\n'.encode()
    path.write_bytes(header + first + "東京,2.50\r\n".encode())
    stream = SeekableCSV(path)
    try:
        assert next(stream) == ["id", "amount"]
        assert next(stream) == ["Москва\r\nулица 5", "1.25"]
        offset = stream.offset
        assert offset == len(header + first)
    finally:
        stream.close()
    stream = SeekableCSV(path, offset)
    try:
        assert list(stream) == [["東京", "2.50"]]
    finally:
        stream.close()


@pytest.mark.parametrize("amount", ["1e1000000", "-1e1000000", "1e-1000000", "sNaN"])
def test_extreme_decimal_is_row_error(amount):
    with pytest.raises(ValueError):
        validate(["id", amount], {"external_id": 0, "amount": 1})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9999999999999999.99", "9999999999999999.99"),
        ("-9999999999999999.99", "-9999999999999999.99"),
        ("1.230000", "1.23"),
        ("0e1000000", "0.00"),
        ("0e-1000000", "0.00"),
    ],
)
def test_money_has_exact_representable_scale_independent_of_context(raw, expected):
    from decimal import getcontext, localcontext

    with localcontext() as context:
        context.prec = 6
        before = context.copy()
        identity, value = validate(["id", raw], {"external_id": 0, "amount": 1})
        assert identity == "id" and value == Decimal(expected)
        assert value.as_tuple().exponent == -2
        assert getcontext().prec == before.prec
        assert getcontext().flags == before.flags
        assert getcontext().traps == before.traps


@pytest.mark.parametrize("mode", ["eof", "early-close", "read-error", "open-error"])
def test_extensionless_xlsx_closes_binary_stream(tmp_path, monkeypatch, mode):
    import builtins
    from zipfile import BadZipFile

    from importdock import parser

    path = tmp_path / "input"
    book = Workbook()
    book.active.append(["id", "amount"])
    book.active.append(["a", "1.25"])
    book.save(path)
    book.close()
    if mode == "open-error":
        path.write_bytes(b"broken ZIP")
    opened = []
    workbooks = []
    original = parser.load_workbook

    def track_open(*args, **kwargs):
        stream = builtins.open(*args, **kwargs)  # noqa: SIM115 — владелец парсер.
        opened.append(stream)
        return stream

    def track_workbook(*args, **kwargs):
        workbook = original(*args, **kwargs)
        workbooks.append(workbook)
        if mode == "read-error":

            def broken(*args, **kwargs):
                yield ("id", "amount")
                raise ValueError("Ошибка чтения записи")

            monkeypatch.setattr(workbook.active, "iter_rows", broken)
        return workbook

    monkeypatch.setattr(parser, "open", track_open, raising=False)
    monkeypatch.setattr(parser, "load_workbook", track_workbook)
    iterator = rows(path, "xlsx")
    if mode == "open-error":
        with pytest.raises(BadZipFile):
            next(iterator)
    else:
        assert next(iterator) == ["id", "amount"]
        assert len(opened) == 1 and not opened[0].closed
        if mode == "read-error":
            with pytest.raises(ValueError, match="Ошибка чтения записи"):
                list(iterator)
        elif mode == "eof":
            assert list(iterator) == [["a", "1.25"]]
        else:
            iterator.close()
    assert len(opened) == 1 and opened[0].closed
    assert all(book._archive.fp is None for book in workbooks)
