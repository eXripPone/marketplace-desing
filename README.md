# Маркетплейс: архитектура и запуск

Учебный проект с архитектурой модульного монолита и минимальным backend на Python / FastAPI. В приложении реализован только `GET /health`. Бизнес-логики, подключения к БД и платёжной интеграции нет.

## Файлы репозитория

```text
marketplace-design/
├── README.md
├── .gitignore
├── architecture/
│   ├── C4 Context.pdf
│   ├── C4 Containers.pdf
│   ├── README.md
│   └── option.md
└── docker/
    ├── main.py
    ├── requirements.txt
    ├── Dockerfile
    └── .dockerignore
```

- [C4 Context](architecture/C4%20Context.pdf): пользователи маркетплейса и внешняя система оплаты.
- [C4 Containers](architecture/C4%20Containers.pdf): веб-приложение, backend и база данных.
- [Описание архитектуры](architecture/README.md): домены, ответственность, данные и взаимодействия; пункты 3–7 задания.
- [Варианты декомпозиции](architecture/option.md): сравнение монолита и микросервисов, компромиссы и выбор; пункты 6–8.
- `docker/main.py`: приложение с endpoint `/health`.
- `docker/requirements.txt`: зависимости Python.
- `docker/Dockerfile`: сборка образа, запуск приложения и проверка состояния.
- `docker/.dockerignore`: исключения из контекста сборки.

Диаграммы показывают целевую систему. Веб-приложение и PostgreSQL указаны только в архитектуре.

## Требования для запуска

Установленный и запущенный Docker Desktop в режиме Linux containers.

Проверить доступность Docker:

```powershell
docker version
```

## Сборка и запуск

```powershell
docker build -t marketplace-backend ./docker
docker run -d --name marketplace-backend -p 127.0.0.1:8000:8000 marketplace-backend
```

Первая команда собирает образ из папки `docker`, вторая запускает контейнер и открывает приложение на локальном порту 8000.

## Проверка health-check

```powershell
curl.exe -i http://localhost:8000/health
```

Ожидаемый HTTP-статус — `200 OK`, тело ответа:

```json
{"status":"ok"}
```

Состояние контейнера и журнал запуска:

```powershell
docker ps --filter name=marketplace-backend
docker logs marketplace-backend
```

Docker также сам проверяет `/health` каждые 30 секунд. После успешной проверки состояние станет `healthy`.

## Остановка и повторный запуск

```powershell
docker stop marketplace-backend
docker start marketplace-backend
```

Если нужно пересобрать приложение:

```powershell
docker stop marketplace-backend
docker rm marketplace-backend
```