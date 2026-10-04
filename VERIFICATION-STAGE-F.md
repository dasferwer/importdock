# Проверка этапа F

До исправления новые parser tests дали 11 failed / 2 passed; два сценария
API/worker с настоящим PostgreSQL тоже падали (XLSX → 422; Decimal Overflow
после первых 20 строк → queued вместо завершения).

Локально использованы Python 3.12.13, зафиксированные зависимости и отдельный
PostgreSQL 17. Полный suite: **46 passed**; 32 проверки parser входят в это число.
Проверены закрытие XLSX на EOF/раннем close/read error/open error, точные границы
numeric(18,2), неизменный Decimal context, extreme exponents, XLSX resume с
checkpoint 1000 → 2005, CSV byte checkpoint, продолжение очереди и retry при
OperationalError. Formula/corrupt ZIP/512 МиБ archive metadata guard проверены.
Ruff, форматирование и Compose config проходят.

Отдельно собран штатный Python 3.11 runtime и запущены API, PostgreSQL, MinIO,
RabbitMQ и worker в своём Compose-проекте с отдельными томами и свободными
localhost-портами. `scripts.format_smoke` использует реальные HTTP, S3 и worker:

| Вход | Результат | Accepted / rejected | Сумма |
|---|---|---|---|
| XLSX, 2005 строк | completed, checkpoint 2005 | 2005 / 0 | 2506.25 |
| CSV, poison amounts после строки 20 | completed, checkpoint 27 | 23 / 4 | 27.50 |
| Следующий CSV | completed, checkpoint 1 | 1 / 0 | 3.50 |

Предпросмотр XLSX проверил первые 20 строк. Четыре errors CSV имеют номера
21–24; оба крайних нуля сохранились как представимые суммы.

CI запускает suite и форматный сценарий на точной опубликованной ревизии,
вместе с существующим сценарием восстановления миллиона строк.
Локальный форматный сценарий не является throughput benchmark; локальный
XLSX resume остановлен после committed chunk, без заявления о SIGKILL XLSX.

Решение использует бинарный file-like интерфейс [openpyxl](https://openpyxl.readthedocs.io/en/stable/_modules/openpyxl/reader/excel.html)
и независимую от контекста операцию [Decimal.copy_abs](https://docs.python.org/3.11/library/decimal.html#decimal.Decimal.copy_abs).
