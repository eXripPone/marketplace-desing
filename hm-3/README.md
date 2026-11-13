# Flight Booking

Два небольших Go-сервиса. **Booking** принимает REST-запросы и хранит бронирования. **Flight** отвечает за рейсы и места. У каждого своя PostgreSQL. Booking обращается к Flight по gRPC. Flight кеширует чтение в Redis Sentinel.

Проект покрывает пункты **1–9** задания. Пункт 10 (Circuit Breaker) не входит в решение.

## Запуск

Включите Docker Desktop и выполните в PowerShell:

```powershell
cd "C: курс\Разработка ПО (Go)\hm-3"
docker compose up --build
```

Или в фоне: `docker compose up --build -d`. Compose запускает базы, Redis и оба приложения. При старте каждого приложения Goose автоматически применяет его SQL-миграции. Первая сборка требует доступ к Docker Hub и прокси модулей Go.

```powershell
docker compose ps
curl.exe -i http://localhost:8083/health
```

REST доступен на порту 8083. gRPC и базы работают во внутренней сети Compose. Для учебного запуска используется общий ключ из Compose. Свой ключ можно задать в PowerShell перед запуском: `$env:FLIGHT_API_KEY = "your-local-key"`. Не публикуйте этот учебный стенд в интернет: REST не требует авторизации пользователя, а gRPC работает без TLS во внутренней сети.

## Быстрая демонстрация

Выполняйте блоки по порядку во втором терминале. Рейсы из учебного набора имеют даты апреля 2026 года; поиск использует дату из запроса, а не текущую дату.

```powershell
cd "C: курс\Разработка ПО (Go)\hm-3"
$base = "http://localhost:8083"
$search = "$base/flights?origin=SVO&destination=LED&date=2026-04-01"
Invoke-RestMethod $search
Invoke-RestMethod $search
$before = Invoke-RestMethod "$base/flights/1"
$before | Format-List
```

На свежей базе у рейса 1 доступно 12 мест. Создаём бронь на два:

```powershell
$body = @{
  user_id = "demo"
  flight_id = 1
  passenger_name = "Ivan Petrov"
  passenger_email = "ivan@example.com"
  seat_count = 2
} | ConvertTo-Json
$booking = Invoke-RestMethod "$base/bookings" -Method Post -ContentType "application/json" -Body $body
$booking | Format-List
Invoke-RestMethod "$base/bookings/$($booking.id)"
Invoke-RestMethod "$base/bookings?user_id=demo"
Invoke-RestMethod "$base/flights/1" | Select-Object id,available_seats
```

Получаем CONFIRMED, сумму `900000` копеек и на два места меньше. Показываем записи в обеих базах:

```powershell
docker compose exec -T booking-db psql -U booking -d bookings -c "SELECT id,flight_id,seat_count,total_price_cents,status FROM bookings WHERE id='$($booking.id)';"
docker compose exec -T flight-db psql -U flight -d flights -c "SELECT booking_id,flight_id,seat_count,status FROM seat_reservations WHERE booking_id='$($booking.id)'; SELECT id,available_seats FROM flights WHERE id=1;"
```

Отмена возвращает два места:

```powershell
Invoke-RestMethod "$base/bookings/$($booking.id)/cancel" -Method Post
Invoke-RestMethod "$base/flights/1" | Select-Object id,available_seats
docker compose exec -T flight-db psql -U flight -d flights -c "SELECT booking_id,status FROM seat_reservations WHERE booking_id='$($booking.id)';"
docker compose logs --tail 30 flight
```

Бронь теперь CANCELLED, резерв RELEASED. В логах Flight видны hit/miss и удаление ключей после изменения. Повторная отмена REST вернёт 409. Повторный gRPC ReleaseReservation безопасен и второй раз места не добавит.

## REST

| Метод | Адрес | Результат |
|---|---|---|
| GET | `/flights?origin=SVO&destination=LED&date=2026-04-01` | Список SCHEDULED. Дата необязательна |
| GET | `/flights/{id}` | Один рейс |
| POST | `/bookings` | Бронь CONFIRMED, HTTP 201 |
| GET | `/bookings/{id}` | Одна бронь |
| GET | `/bookings?user_id=demo` | Брони пользователя |
| POST | `/bookings/{id}/cancel` | Бронь CANCELLED |
| GET | `/health` | Booking запущен и подключён к своей БД |

Время передаётся в RFC3339, день поиска и уникальности рейса считается по UTC. Деньги — целые копейки: `total_price_cents = seat_count * price_cents`. REST-ошибка содержит `code` и `message`.

| gRPC | REST | Пример |
|---|---:|---|
| INVALID_ARGUMENT | 400 | Неверные дата, IATA или число мест |
| NOT_FOUND | 404 | Нет рейса или брони |
| RESOURCE_EXHAUSTED | 409 | Недостаточно мест |
| FAILED_PRECONDITION | 409 | Рейс отменён или бронь уже отменена |
| ALREADY_EXISTS | 409 | Тот же UUID резерва с другими параметрами |
| UNAUTHENTICATED | 502 | Booking передал неверный ключ Flight |
| UNAVAILABLE / DEADLINE_EXCEEDED | 503 | Flight временно недоступен |

## Проверки

```powershell
docker compose exec booking /app/check verify
```

Проверка использует демо-рейсы 1 и 2. На рейсе 1 должно быть не меньше двух свободных мест, на рейсе 2 — одно. Проверяются REST, ключ для всех RPC, конкурентное резервирование последнего места, повтор по UUID, цена покупки и инвалидирование кеша при резерве, возврате и изменении рейса. Проверочные брони отменяются в конце, история в БД сохраняется. Модульные тесты политики retry выполняются автоматически при сборке.

Проверка временной недоступности Flight:

```powershell
docker compose stop flight
curl.exe -i http://localhost:8083/flights/1
docker compose logs --tail 12 booking
docker compose start flight
```

Ожидается HTTP 503 и три попытки в логах. После запуска Flight дождитесь healthy в `docker compose ps` и повторите запрос. Не выполняйте этот шаг одновременно с интеграционными тестами.

Проверка Sentinel — команды работают и после предыдущего переключения:

```powershell
$masterIp = (docker compose exec -T sentinel-1 redis-cli --raw -p 26379 SENTINEL get-master-addr-by-name flights)[0]
$masterService = if ($masterIp -eq "172.29.83.11") { "redis-master" } else { "redis-replica" }
docker compose stop $masterService
```

Повторяйте команду ниже, пока она не покажет другой адрес (обычно несколько секунд):

```powershell
docker compose exec -T sentinel-1 redis-cli -p 26379 SENTINEL get-master-addr-by-name flights
```

Теперь дважды запросите рейсы, проверьте кеш в логах и верните остановленный контейнер:

```powershell
$search = "http://localhost:8083/flights?origin=SVO&destination=LED&date=2026-04-01"
Invoke-RestMethod $search
Invoke-RestMethod $search
docker compose logs --tail 15 flight
docker compose start $masterService
```

Sentinel переключает мастер, а клиент Flight узнаёт адрес через Sentinel. Если Redis недоступен, Flight читает PostgreSQL. Старый мастер после возврата становится репликой. Redis хранит только восстанавливаемый кеш. Для Redis и Sentinel выделена внутренняя сеть с постоянными адресами: остановка контейнера не мешает Sentinel определить отказ. Сеть кеша использует подсеть `172.29.83.0/24`.

## Как устроены данные и код

- [ER-диаграмма и объяснение 3NF](docs/er.md).
- `proto/flight.proto` — контракт с Timestamp, enum и описанием ошибок.
- `internal/flight/service.go` — запросы, резерв и возврат.
- `internal/flight/cache.go` — Cache-Aside, TTL 5 минут и очистка ключа рейса, поиска по его дате и поиска без даты.
- `internal/booking/http.go` — REST. `service.go` в той же папке — последовательность действий и запись в БД.
- `internal/booking/client.go` — ключ в gRPC metadata и до трёх попыток. Между попытками 100 и 200 мс, время одной попытки ограничено секундой. Повторы только для UNAVAILABLE и DEADLINE_EXCEEDED.
- `migrations/` — миграции Goose для раздельных БД.

В Flight резерв и остаток меняются в одной транзакции. `SELECT ... FOR UPDATE` блокирует строку рейса. UUID брони — первичный ключ резерва; транзакционная блокировка по UUID не даёт двум одинаковым запросам одновременно создать запись. Возврат тоже выполняется атомарно.

Booking сначала вызывает GetFlight, затем ReserveSeats. UUID создаётся один раз на REST-запрос, поэтому gRPC-повторы отправляют тот же UUID. Только успешный резерв позволяет записать CONFIRMED. При отмене сначала возвращаются места, затем меняется статус брони.

У сервисов разные БД, общей SQL-транзакции нет. На случай потери ответа или сбоя между двумя записями Booking хранит короткую запись в `flight_operations`. Если создание не завершилось, он освобождает возможный резерв; если прервалась отмена — доводит её до конца. Проверка восстановления запускается раз в 5 секунд для записей старше 30 секунд. Восстановление после межсервисного сбоя выполняется не мгновенно: до завершения компенсации места могут быть заняты. Это не добавляет отдельный сервис или очередь. Повтор REST POST сам по себе создаёт новую бронь; идемпотентен внутренний gRPC-вызов с тем же UUID.

## Кодогенерация

Dockerfile сам запускает protoc и тесты. Готовые Go-файлы из контракта также есть в `generated/`, чтобы можно было открыть их в редакторе и запустить `go test ./...` на компьютере.

Если изменили .proto, обновите локальные файлы:

```powershell
docker build --target generated --output "type=local,dest=." .
```

Остановить систему с сохранением базы: `docker compose down`.
