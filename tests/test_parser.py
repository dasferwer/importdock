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
