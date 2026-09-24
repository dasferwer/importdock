import csv
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook


def rows(path, format):
    if format == "csv":
        with open(path, encoding="utf-8-sig", newline="") as stream:
            yield from csv.reader(stream)
    else:
        workbook = load_workbook(path, read_only=True, data_only=False)
        try:
            for row in workbook.active.iter_rows(values_only=True):
                yield ["" if value is None else str(value) for value in row]
        finally:
            workbook.close()


def columns(header, mapping):
    if len(set(header)) != len(header):
        raise ValueError("Заголовки колонок должны быть уникальными")
    if set(mapping) != {"external_id", "amount"}:
        raise ValueError("Нужно сопоставить external_id и amount")
    if mapping["external_id"] == mapping["amount"]:
        raise ValueError("Поля должны использовать разные колонки")
    try:
        return {key: header.index(value) for key, value in mapping.items()}
    except ValueError as exc:
        raise ValueError("В файле нет указанной колонки") from exc


def validate(row, indices):
    try:
        identity = row[indices["external_id"]].strip()
        raw = row[indices["amount"]].strip()
        amount = Decimal(raw)
    except (IndexError, InvalidOperation) as exc:
        raise ValueError("Нет поля или сумма не является числом") from exc
    if not identity or len(identity) > 120:
        raise ValueError("Идентификатор должен содержать от 1 до 120 символов")
    if (
        not amount.is_finite()
        or abs(amount) >= Decimal("1e16")
        or amount != amount.quantize(Decimal("0.01"))
    ):
        raise ValueError("Нужна конечная сумма с точностью не более двух знаков")
    return identity, amount
