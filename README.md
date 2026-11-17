# Creative Shygys — тестовый бэкенд

FastAPI / Pydantic v2, PostgreSQL 16 / PostGIS, Redis / ARQ и S3.

## Подключение фронтенда

- API стенда — http://10.38.0.94:8000/api/v1
- Swagger — http://10.38.0.94:8000/docs
- OpenAPI — http://10.38.0.94:8000/openapi.json
- Финальный файл схемы — [openapi.json](openapi.json).

API слушает `0.0.0.0:8000`. Проверены запросы к сетевому адресу хоста и CORS с credentials для `http://localhost:3000`, `http://127.0.0.1:3000` и `http://10.38.0.94:3000`. Для другого адреса фронтенда задайте `CORS_ORIGINS` JSON-массивом и перезапустите API. Доступ с другой машины зависит от сетевого маршрута до хоста.

Вход — `POST /api/v1/auth/login` с `email` и `password`. Дальше передавайте `Authorization: Bearer <access_token>`. Публичный каталог работает без токена.

## Демологины

Для всех тестовых аккаунтов установлен пароль `yOiV2BACE3Sc8JDxVXHBOm4w7fIVJJDL`.

| Роль | Email |
|---|---|
| creator | creator@demo.creative-shygys.example.com |
| moderator | moderator@demo.creative-shygys.example.com |
| content_editor | content_editor@demo.creative-shygys.example.com |
| analyst | analyst@demo.creative-shygys.example.com |
| center_curator | center_curator@demo.creative-shygys.example.com |
| admin | admin@demo.creative-shygys.example.com |
| org_rep | org_rep@demo.creative-shygys.example.com |


Куратор ограничен демо-центром в Өскемене. Все аккаунты и данные вымышлены. Доступ к production они не предоставляют.

## Состав базы и запуск

Сидер создаёт 80 демокреаторов по восьми городам и населённым пунктам ВКО, 126 работ, 12 проектов, 18 новостей, 18 событий и 8 мер поддержки, историю рассмотрения заявок и данные BI. Географические центры приблизительные. Официальный справочник 43 ОКЭД импортируется отдельно из согласованного XLSX.

Для новой пустой PostgreSQL/PostGIS базы задайте `DATABASE_URL`, для Redis — `REDIS_URL`.

```bash
uv sync --locked
uv run alembic upgrade head
uv run python -m scripts.seed_demo --count 80
uv run python -m scripts.run_demo
```

`run_demo` запускает API и воркер. При заданных переменных использует их, иначе подключается к локальной демобазе `shygys_demo` на порту 55432 и Redis на порту 56379. Сидер защищён от повторного заполнения и от записи в занятую базу. Пароли новой установки генерируются в `DEMO_ACCESS.md`; таблица выше относится к текущему стенду.

## BI и отчёты

`/analytics/overview`, `/dynamics`, `/funnel`, `/geography` и `/compare` принимают `date_from`, `date_to`, `district_id`, `category_id`, `oked_code`, `legal_status`, `is_resident`. По умолчанию временная зона расчёта — Asia/Almaty. Обзор считает текущие одобренные записи, созданные не позднее конца периода, независимо от публичности. Динамика считает регистрации внутри периода. Доля одобрения и время модерации считаются по решениям заявок профилей внутри периода. Активные резидентства проверяются на текущую дату. Историческое сравнение использует текущий статус профиля и дату создания, это не восстановление полного состояния реестра на прошлую дату.

В ответах есть `generated_at` и отметки малой выборки. В географических, категориальных и ОКЭД-агрегатах группы менее пяти скрываются. CSV/XLSX выдаются по `/analytics/export/{fmt}` только для аналитика/администратора и содержат агрегаты без контактов. PDF/PNG используют настроенный Browserless.

Внутренние уведомления — `/me/notifications`, прочтение — `PUT /me/notifications/{id}/read`. Запрос экспорта/закрытия — `POST /me/data-requests/export` или `/close`. Согласованная обработка запросов доступна администратору. Письма о модерации ставятся в PostgreSQL outbox и отправляются воркером через SMTP с повторами.

## Проверка

```bash
uv run ruff check .
SHYGYS_INTEGRATION=1 uv run python -m pytest -q
```

Интеграционные тесты запускайте с отдельными тестовыми `DATABASE_URL` и `REDIS_URL`, после применения миграций. На чистой базе пройдены приёмочные тесты; протокол обновляется в ACCEPTANCE.md. Матрица серверных сценариев AC-01–AC-30 — [ACCEPTANCE.md](ACCEPTANCE.md). Требования к мобильному UI, HTML и графикам проверяются в проекте Next.js.

Подробные настройки S3, AlemAI, SMTP и Browserless — [DEVELOPMENT.md](DEVELOPMENT.md). На текущем стенде подключены реальные MinIO, Mailpit SMTP и внешний Browserless. Проверены загрузка оригинала, обработка WebP, запрет анонимного доступа к S3, письма подтверждения и модерации и генерация PDF/PNG через API. ClamAV отключён через `ENABLE_CLAMAV=false`; включение сервиса описано в DEVELOPMENT.md. Реальный AlemAI ранее проверен через очередь воркера.


Mailpit доступен по `http://localhost:8025`, консоль MinIO — `http://localhost:9001`. Сервисы запускаются через `uv run python -m scripts.run_test_services`, API и воркер — через `uv run python -m scripts.run_demo`. Для сквозной проверки используйте `uv run python -m scripts.check_real_services --require-browserless`.

### Безопасность перед деплоем

Установите `ENV=production` и перезапустите API: `/docs`, `/redoc`,
`/docs/oauth2-redirect` и `/openapi.json` возвращают 404. Репозиторный
`openapi.json` остаётся доступным для генерации типов при сборке фронтенда;
не публикуйте корень репозитория как статические файлы.
Debug выключен; необработанные ошибки возвращают общий JSON 500, подробности
остаются в серверном журнале.

Redis-лимиты общие для маршрутов с `/api/v1` и без него:
login — 30/15 минут на IP и 8/15 минут на email; register — 8/час на IP
и 3/час на email; reset-password — 10/15 минут на IP;
translations/generate — 20/час на пользователя (включая старый маршрут jobs).
Превышение: 429 с Retry-After; недоступный Redis: 503 без выполнения операции.
При reverse proxy доверяйте forwarded-заголовкам только от IP своего прокси
(`--forwarded-allow-ips` Uvicorn), не используйте `*` на публичном API.

Медиа: разрешены только JPEG/PNG/WebP; Pillow определяет формат по содержимому,
сверяет заявленный MIME, проверяет структуру и полностью декодирует изображение.
Лимит по умолчанию 12 MiB, максимум 24 млн пикселей, анимация запрещена.
Расширения разрешены только .jpg/.jpeg/.png/.webp; двойные расширения с другими
суффиксами запрещены. SVG, HTML, PHP, shell и исполняемые файлы отклоняются до S3.
На входном reverse proxy также задайте ограничение тела запроса (например,
`client_max_body_size 13m;` для Nginx): multipart разбирается до проверки
размера файла обработчиком. ClamAV остаётся отключённым по согласованной
настройке тестового стенда; проверка формата не заменяет антивирус.

### Резервные копии PostgreSQL

Готовый скрипт: `scripts/backup_db.sh`. Требуются PostgreSQL client tools
совместимой с сервером версии, `flock`, `find`. Используется сжатый custom-format
дамп (`pg_dump -Fc`), права каталога 700, архива 600, блокировка параллельных
запусков. Только после успешного дампа и проверки оглавления удаляются
собственные архивы старше 14 суток. Пароль храните в `PGPASSFILE` с правами 600;
скрипт намеренно не исполняет содержимое `.env`.

Пример запуска (поменяйте пути и реквизиты под сервер):
```bash
PGHOST=127.0.0.1 PGPORT=5432 PGUSER=shygys PGDATABASE=shygys \
PGPASSFILE=/etc/shygys/backup.pgpass BACKUP_DIR=/var/backups/creative-shygys \
/srv/creative-shygys/scripts/backup_db.sh
```

Для ежедневного запуска в 03:15 добавьте в crontab пользователя бэкапа:
```cron
15 3 * * * PGHOST=127.0.0.1 PGPORT=5432 PGUSER=shygys PGDATABASE=shygys PGPASSFILE=/etc/shygys/backup.pgpass BACKUP_DIR=/var/backups/creative-shygys /srv/creative-shygys/scripts/backup_db.sh >> /var/log/shygys-backup.log 2>&1
```

Восстановление в заранее созданную пустую БД с доступным PostGIS:
`pg_restore --exit-on-error --no-owner --dbname=shygys_restore /path/archive.dump`.
Дамп содержит данные одной БД; роли кластера и объекты S3 резервируются отдельно.
Cron в текущей среде не устанавливался. Ежедневное расписание даёт интервал
потери данных до 24 часов; время восстановления зависит от размера базы.
