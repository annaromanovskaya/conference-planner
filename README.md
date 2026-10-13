# ConfPlanner

ConfPlanner — персональный self-hosted Telegram-агент для поиска научных конференций,
контроля дедлайнов и заметок владельца. Один deployment обслуживает одного владельца;
сообщения остальных пользователей отклоняются до запуска обработчиков.

## Features

- Сам Telegram-бот является AI-чатом: ordinary text сразу получает ответ агента, отдельной
  кнопки «AI-чат» нет.
- Ollama Cloud, локальный Ollama и локальный OpenAI-compatible API с выбором модели через
  `/model` без перезапуска.
- Ограниченный agent loop с внутренним MCP v2, памятью диалога и SQL-графом знаний.
- Пользовательские заметки и правила: например, «эта конференция приоритетная» или
  «не предлагай мероприятия в США».
- Любое агентское изменение сначала становится expiring pending action и выполняется только
  после подтверждения владельцем в Telegram.
- Поиск через внутренний SearXNG, SSRF-safe загрузка страниц, provenance дедлайнов,
  дедупликация, мониторинг и напоминания.

## Architecture

Один async Python-процесс запускает aiogram polling, APScheduler и MCP Streamable HTTP на
`127.0.0.1`. MCP-порт не публикуется Docker Compose. SQLite остаётся источником истины для
конференций, связей, истории чата, памяти, выбранной модели и pending actions. SearXNG работает
вторым внутренним сервисом. Модель не получает shell, filesystem, raw SQL, secrets,
произвольный fetch URL или unrestricted web tool.

## How it works

Агент собирает системную политику, релевантную память, summary и последние сообщения. Модель
может ответить сразу либо вызвать один из строго описанных MCP-инструментов. Аргументы проходят
JSON Schema validation. Чтение выполняется сразу; `propose_*` лишь создаёт pending action.
Подтверждение вызывает детерминированный Python-сервис ровно один раз. Turn ограничен числом
шагов, tool calls, размером контекста/результатов и общим timeout.

Поиск конференций остаётся source-backed: Python строит запросы, SearXNG возвращает реальные
URL, безопасный fetcher получает ограниченный HTML, а модель извлекает только факты страницы.
Pydantic валидирует структурированный ответ. Ollama Cloud не гарантирует provider-enforced
structured output, поэтому в приложении сохранены validation и bounded retries.

## Requirements

- Docker Engine и Compose v2;
- Telegram bot token и numeric Telegram ID владельца;
- хотя бы один model profile: Ollama Cloud key, локальный Ollama или локальный
  OpenAI-compatible endpoint;
- для разработки: Python 3.12+ и `uv`.

## Quick start

```bash
git clone YOUR_REPOSITORY_URL ConfPlanner
cd ConfPlanner
cp .env.example .env
# заполните TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID и model profile
docker compose up -d
docker compose logs -f conference-scout
```

При старте Alembic автоматически обновляет свежую или известную legacy-базу без удаления
существующих тем, конференций, источников и истории.

## Telegram bot creation

Создайте бота через `@BotFather`, запишите token в `TELEGRAM_BOT_TOKEN`, а свой numeric ID —
в `OWNER_TELEGRAM_ID`. Не отправляйте token или model API key в чат бота.

## Ollama and local model configuration

### Ollama Cloud

```dotenv
OLLAMA_CLOUD_BASE_URL=https://ollama.com
OLLAMA_CLOUD_API_KEY=YOUR_OLLAMA_CLOUD_API_KEY
```

Cloud показывает только пересечение ответа `/api/tags` и настроенного allowlist. Free usage credits
can be used with указанными в `.env.example` моделями; доступность и условия тарифа
могут измениться на стороне Ollama. Добавление платных credits не выполняется приложением.

### Локальный Ollama

Запустите Ollama на host и разрешите соединение с контейнера. В Compose Linux-host доступен как
`host.docker.internal` благодаря mapping `host-gateway`:

```dotenv
OLLAMA_LOCAL_BASE_URL=http://host.docker.internal:11434
OLLAMA_LOCAL_API_KEY=
```

При запуске без Docker обычно используйте `http://127.0.0.1:11434`. Если ваш endpoint защищён,
задайте `OLLAMA_LOCAL_API_KEY`.

### Local OpenAI-compatible API

```dotenv
LOCAL_OPENAI_BASE_URL=http://host.docker.internal:8000
LOCAL_OPENAI_API_KEY=
```

Подойдут серверы, реализующие `/v1/models` и `/v1/chat/completions`. Ключ опционален. Профиль
скрыт из `/model`, пока URL не настроен.

## Telegram commands

- `/start` — краткая справка; после неё просто пишите запросы обычным текстом;
- `/model` — выбрать profile/model и проверить tool-calling capability;
- `/new` — начать новый диалог, не удаляя долгосрочную память;
- `/memory` — показать активные заметки и правила;
- `/forget ID` — предложить удалить запись памяти с подтверждением;
- `/help` — подсказка по командам.

Модель без tool calling остаётся доступной с пометкой «только чат». Исчезнувшая из API модель
не выбирается повторно; выполните `/model` и выберите актуальную.

## Memory and confirmations

История диалога хранится в SQLite и при росте заменяется безопасным summary. Долгосрочная память
хранит исходный текст, валидированное правило, scope (`GLOBAL`, `TOPIC`, `CONFERENCE`) и
provenance. SQL knowledge graph проецирует связи тем, конференций, дедлайнов, источников,
подписок и заметок без отдельной graph DB.

Добавление темы/источника, tracking/archive, сохранение/удаление памяти и discovery требуют
кнопки «Подтвердить». Просроченное, отменённое, чужое или уже выполненное действие не исполняется.

## Model smoke check

Smoke-команда делает реальный запрос и может расходовать Cloud credits, поэтому обычные тесты её
не запускают. Выполняйте только явно:

```bash
uv run python scripts/smoke_model.py --profile ollama-local --model llama3.2
uv run python scripts/smoke_model.py --profile ollama-cloud --model gpt-oss:20b
```

Profile IDs: `ollama-cloud`, `ollama-local`, `openai-local`. Команда проверяет список моделей и
один harmless tool call, но никогда не печатает ключ или upstream response body.

## SearXNG

Compose запускает SearXNG без host-порта. Приложение обращается к нему по
`http://searxng:8080`. Внешние страницы остаются недоверенными данными и не могут менять policy
или список инструментов агента.

## Configuration

Все переменные перечислены в `.env.example`. Основные лимиты: `AGENT_MAX_STEPS`,
`AGENT_MAX_TOOL_CALLS_PER_STEP`, `AGENT_TIMEOUT_SECONDS`, `AGENT_MAX_CONTEXT_CHARS` и
`AGENT_MAX_TOOL_RESULT_CHARS`. MCP должен оставаться на `MCP_HOST=127.0.0.1`. Изменения `.env`
требуют рестарта контейнера; выбор через `/model` сохраняется без рестарта.

## Docker deployment

```bash
docker compose up -d
docker compose ps
docker compose logs -f conference-scout
```

Приложение использует long polling и не публикует входных портов. Контейнер работает non-root,
с read-only root filesystem, dropped capabilities, `no-new-privileges`, writable `/data` и
tmpfs `/tmp`. Ни MCP, ни SearXNG наружу не опубликованы.

## Trusted sources

`DOMAIN example.org` добавляет `site:`-ограничение, а `PAGE https://example.org/events` — каталог
для проверки. Trusted не означает official: вторичные даты сохраняются с соответствующим
provenance и verification flag.

## Topics

Темы можно добавить разговором с агентом или старым shortcut «Мои темы». Каноническое имя,
описание и keywords валидируются, а запись появляется только после подтверждения.

## Discovery

Попросите агента найти конференции или нажмите shortcut «Найти конференции». Агент предложит
запуск за конкретные годы; scheduled discovery проверяет текущий и следующий календарный год.
Ошибки изолируются на уровне query и candidate.

## Deadline tracking

Tracking включает ежедневный monitoring и reminders. Дедлайн хранит source URL и verification
flag. Изменение даты обновляет текущее значение и добавляет deadline history.

## Backups

Перед файловой копией остановите writer:

```bash
docker compose stop conference-scout
docker run --rm -v confplanner_conference_data:/data -v "$PWD":/backup alpine \
  cp /data/conference_scout.db /backup/conference_scout.db
docker compose start conference-scout
```

Имя volume уточните через `docker volume ls`. Для восстановления остановите сервис, замените
`/data/conference_scout.db`, сохраните владельца/права и снова запустите приложение. Всегда
проверяйте backup отдельно до обновления.

## Security

Secrets находятся только в ignored `.env`, не сохраняются в БД, prompts, MCP results или
Telegram errors. Не коммитьте `.env`. Fetcher разрешает лишь HTTP(S) public destinations,
проверяет DNS/redirects и блокирует private, loopback, link-local и metadata addresses. MCP
доступен только внутри container loopback. Healthcheck выполняет только `SELECT 1` и не вызывает
модель, поэтому не расходует credits.

## Development

```bash
uv sync --python 3.12 --extra dev
uv run ruff format .
uv run ruff check .
uv run pytest -q
docker compose config --quiet
```

SQLite async tests могут требовать обычных thread permissions из-за worker thread `aiosqlite`.

## Testing

Обычный suite использует fake model/search/fetch/Telegram boundaries и временные SQLite-базы —
credentials, интернет и credits не нужны. Integration tests покрывают Alembic compatibility,
discovery, monitoring, MCP handshake и model→tool→confirmation→domain flow.

## Limitations

JavaScript-only страницы не рендерятся. Качество extraction зависит от модели. Локальный endpoint
должен быть доступен контейнеру и иметь достаточную RAM/VRAM. Это намеренно single-owner service;
MCP не является публичным multi-tenant API.

## Roadmap

- Optional browser fetcher для JavaScript-only сайтов.
- Улучшенное моделирование conference series и повторяющихся событий.
- Более точный semantic retrieval поверх текущего SQL source of truth.

## License

MIT. См. `LICENSE`.
