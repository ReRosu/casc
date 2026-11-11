# casc — каскадирование платежей (PayMaster + Pally)

Сервис проводит платёж через нескольких провайдеров по очереди. Если провайдер не смог провести
платёж или выдать реквизиты, запрос автоматически уходит к следующему. При этом сервис не каскадит
там, где это опасно: жёсткие отказы, неизвестный исход списания, отказ на странице оплаты.

Дизайн и обоснование решений: **[docs/DESIGN.md](docs/DESIGN.md)**.

Стек: Python 3.14, FastAPI, PostgreSQL, SQLAlchemy 2 (async), Alembic, taskiq + Redis, Poetry,
pydantic-settings, Docker.

## Быстрый старт

```sh
make up      # postgres, redis, миграции, api :18000, worker, scheduler, эмулятор :18001
make demo    # 19 сценариев каскада против эмулятора
```

Swagger: http://localhost:18000/docs. Список тестовых сценариев эмулятора: http://localhost:18001/.

Без Docker для приложения (Postgres и Redis всё равно из compose):

```sh
docker compose up -d postgres redis
poetry install && cp .env.example .env
poetry run alembic upgrade head
poetry run uvicorn app.emulator.app:app --port 18001 &
poetry run uvicorn app.main:app --port 18000 &
poetry run taskiq worker app.worker.broker:broker &
poetry run taskiq scheduler app.worker.broker:scheduler &
```

## Тесты

```sh
make test    # ruff + 129 тестов (unit + integration; integration нужен Postgres из compose)
```

- `tests/unit` — классификация отказов, state machine, роутинг, circuit breaker, адаптеры
  (HTTP мокается через respx).
- `tests/integration` — движок каскада на настоящем Postgres с управляемыми фейковыми провайдерами:
  каскад, HARD-стоп, UNKNOWN без двойного списания, осиротевшие ссылки, поздний успех и возврат,
  идемпотентность, в том числе параллельные запросы с одним ключом.

## API

```http
POST /v1/payments
Idempotency-Key: 6f1c…

{"flow": "card", "order_id": "42", "amount": "1500.00", "description": "Заказ 42",
 "card": {"pan": "4000000000000002", "expiry": "12/29", "cvc": "123"}}
```

```jsonc
{
  "status": "succeeded",
  "attempts": [
    {"seq": 1, "provider": "paymaster_a", "mode": "h2h", "status": "declined",
     "decline_code": "TransactionDeclined", "decline_class": "soft"},
    {"seq": 2, "provider": "paymaster_b", "mode": "h2h", "status": "succeeded"}
  ]
}
```

| Метод | Путь | |
|---|---|---|
| `POST` | `/v1/payments` | Создать платёж: `flow=card` (карта) или `flow=link` (ссылка/реквизиты, `method=sbp\|bankcard`) |
| `GET` | `/v1/payments/{id}` | Статус, попытки, `next_action` (3DS или переадресация) |
| `POST` | `/v1/payments/{id}/refund` | Полный возврат |
| `GET/POST` | `/v1/return/{attempt_id}` | Возврат браузера с hosted-страницы или 3DS (ACS) |
| `POST` | `/webhooks/pally` | Postback Pally (подпись + сверка статуса через API) |
| `POST` | `/webhooks/paymaster/{instance}/{attempt_id}/{hmac}` | Уведомление PayMaster (подпись в URL, статус сверяется через API) |
| `GET` | `/v1/providers` | Маршруты и состояние circuit breaker |
| `POST` | `/v1/providers/{name}/circuit/reset` | Ручное закрытие breaker'а |

## Эмулятор и тестовые данные

Адаптеры работают с эмулятором как с настоящим API: те же пути, форматы, авторизация. На реальных
провайдеров сервис переключается переменными `PAYMASTER_A__BASE_URL`, `PALLY__BASE_URL` и т. д.

**Карты (`flow=card`, PayMaster):**

| PAN | Поведение |
|---|---|
| `4111111111111111` | успех |
| `4000000000000002` | мягкий отказ на A → успех на B |
| `4000000000000341` | мягкий отказ на A и B → hosted Pally |
| `4000000000009995` | `InsufficientFunds` (HARD) |
| `4000000000000127` | `RejectedByFraud` (HARD) |
| `4000000000003220` | 3DS (`PaRes=ok` → успех, `fail` → `ThreeDSecureFailed`) |
| `4000000000003238` | 3DS на A, затем мягкий отказ → hosted Pally |
| `4000000000000119` | HTTP 500 на A → B |
| `4000000000000259` | таймаут на A, платёж не создан → B |
| `4000000000000267` | таймаут на A, деньги списаны → успех без второго списания |

**Копейки суммы (`flow=link`, Pally `bill/create`):** `.01` направление недоступно (SOFT) ·
`.02` таймаут, но счёт создан (осиротевший счёт) · `.03` 401 (CONFIG) · `.04` invalid_amount (HARD) ·
`.05` 500 · `.06` transaction_rejected.

**Падение провайдера:** `POST :18001/{paymaster-a|paymaster-b|pally}/_admin/outage?seconds=60`.

## Структура

```
app/
├── main.py              # FastAPI + lifespan
├── core/                # config (pydantic-settings, get_settings с lru_cache), redis
├── db/                  # db.py (engine/sessionmaker — лениво), models/ по файлу на таблицу
├── domain/              # enums, классификация отказов, state machine, ProviderResult — без SQLAlchemy
├── providers/           # адаптеры PayMaster и Pally, реестр
├── routing/             # маршрут и circuit breaker
├── cascade/             # движок каскада, inbox вебхуков, сборка зависимостей
├── api/                 # HTTP-ручки и схемы
├── worker/              # taskiq: брокер, задачи, reconcile
└── emulator/            # эмулятор провайдеров
```
