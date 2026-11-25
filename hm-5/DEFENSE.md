# Короткая демонстрация

Все команды выполняются в PowerShell в папке `hm-5`.

## 1. Запустить и объяснить систему

Скажи: «Я сделал аналитику онлайн-кинотеатра. Сервис принимает событие, отправляет его в Kafka. ClickHouse читает события. Отдельный сервис считает метрики и сохраняет их в две базы. Grafana показывает метрики, а S3 хранит ежедневные файлы».

```powershell
cd "C:\3 курс\Архитектура\marketplace-design\hm-5"
docker compose up --build -d
docker compose ps -a
docker compose logs --tail 20 producer migrate aggregation
```

Покажи: работающие контейнеры; у `migrate` код завершения 0.

## 2. Отправить своё событие

```powershell
$eventId = [guid]::NewGuid().ToString()
$session = [guid]::NewGuid().ToString()
$event = @{
  event_id = $eventId
  user_id = "defense-user"
  movie_id = "defense-movie"
  event_type = "VIEW_STARTED"
  timestamp = [DateTime]::UtcNow.ToString("o")
  device_type = "DESKTOP"
  session_id = $session
  progress_seconds = 0
}
Invoke-RestMethod http://localhost:8005/events -Method Post -ContentType "application/json" -Body ($event | ConvertTo-Json)
```

Скажи: «Ответ с ID означает, что Kafka подтвердила запись. Ключ сообщения — пользователь. Его события идут в одну партицию».

Через несколько секунд покажи реальную запись в ClickHouse:

```powershell
docker compose exec -T clickhouse clickhouse-client --user cinema --password cinema --query "SELECT * FROM cinema.events WHERE event_id = '$eventId' FORMAT Vertical"
docker compose exec -T kafka kafka-topics --bootstrap-server kafka:9092 --describe --topic movie-events
Invoke-RestMethod http://localhost:8085/subjects/movie-events-value/versions/latest
```

Ожидается запись со всеми полями, топик с тремя партициями и схема с номером версии.

## 3. Завершить просмотр и посчитать метрики

```powershell
$event.event_id = [guid]::NewGuid().ToString()
$event.event_type = "VIEW_FINISHED"
$event.timestamp = [DateTime]::UtcNow.ToString("o")
$event.progress_seconds = 600
Invoke-RestMethod http://localhost:8005/events -Method Post -ContentType "application/json" -Body ($event | ConvertTo-Json)
Start-Sleep -Seconds 5
$day = [DateTime]::UtcNow.ToString("yyyy-MM-dd")
Invoke-RestMethod "http://localhost:8006/recalculate/$day" -Method Post
docker compose exec -T postgres psql -U cinema -d cinema -c "SELECT day,metric,dimension,value,computed_at FROM daily_metrics WHERE day='$day' ORDER BY metric,dimension;"
```

Скажи: «DAU — сколько разных пользователей было за день. Среднее время берётся из завершённых просмотров. Конверсия — число завершений, делённое на число начал. Метрики не будут равны только моему примеру: параллельно работает генератор».

Повтори пересчёт и покажи, что ключи не дублируются:

```powershell
Invoke-RestMethod "http://localhost:8006/recalculate/$day" -Method Post
docker compose exec -T postgres psql -U cinema -d cinema -c "SELECT day,metric,dimension,count(*) FROM daily_metrics GROUP BY day,metric,dimension HAVING count(*) > 1;"
```

Ожидается 0 строк. Объясни: «В PostgreSQL используется UPSERT: строка обновляется по ключу».

## 4. Показать Grafana и retention

Открой http://localhost:3005/d/cinema, логин `admin`, пароль `admin`.

Скажи: «Строка — пользователи, которые впервые начали просмотр в этот день. Day 1 показывает, какая их доля вернулась на следующий день. Day 7 — через неделю. Будущие дни пока неизвестны, поэтому там прочерк. Таблица читает готовые метрики ClickHouse».

Покажи цветную таблицу, DAU, конверсию и топ фильмов.

## 5. Показать экспорт

```powershell
Invoke-RestMethod "http://localhost:8006/export/$day" -Method Post
docker compose exec -T aggregation python -c "from app.common import s3; print(s3().get_object(Bucket='movie-analytics', Key='daily/$day/aggregates.json')['Body'].read().decode())"
Invoke-RestMethod "http://localhost:8006/export/$day" -Method Post
```

Скажи: «Экспорт читает PostgreSQL. В JSON есть все метрики, дата и время расчёта. Повторный вызов перезаписывает этот же файл. Раз в день система автоматически выгружает вчерашние метрики».

## 6. Показать ошибку и тесты

```powershell
$event.progress_seconds = -1
try {
  Invoke-RestMethod http://localhost:8005/events -Method Post -ContentType "application/json" -Body ($event | ConvertTo-Json)
} catch {
  $_.Exception.Response.StatusCode
  $_.ErrorDetails.Message
}
docker compose run --build --rm tests
```

Покажи ошибку 422 и итог `ALL ... CHECKS PASSED`.

Скажи: «В тестовом наборе три пользователя начали просмотр, двое завершили. Поэтому DAU равен 3, конверсия — 2/3. Вернулся один на следующий день и двое через неделю: D1 — 1/3, D7 — 2/3. Тест проверяет эти значения в базе, а не только успешный ответ API».

## 7. Где показать код

1. `schemas/movie-event-v1.avsc` — поля и типы события.
2. `app/producer.py`, функция `publish` — проверка, Avro, Kafka и подтверждение доставки.
3. `migrations/clickhouse.sql` — Kafka Engine, Materialized View и постоянная таблица.
4. `app/aggregate.py`, `calculate` — SQL метрик и сохранение в PostgreSQL.
5. Там же `export_day` и `scheduler` — S3 и запуск по расписанию.
6. `tests/e2e.py` — события с известными результатами и проверки всей цепочки.

Короткие ответы:

- **Зачем Kafka?** Разделить приём событий и их обработку. ClickHouse читает поток независимо от HTTP-сервиса.
- **Зачем две базы?** ClickHouse считает аналитику по событиям, PostgreSQL хранит готовые результаты для других приложений и экспорта.
- **Что такое когорта?** Пользователи с одинаковой датой первого просмотра.
- **Что при повторе?** Идентичное событие убирается при чтении через `FINAL`, метрики обновляются по ключу, S3-файл перезаписывается.
- **Что при сбое?** Есть retry и следующий цикл расчёта. Однако один Kafka-брокер не обеспечивает кластерную отказоустойчивость; пункт на десятый балл не включён.
