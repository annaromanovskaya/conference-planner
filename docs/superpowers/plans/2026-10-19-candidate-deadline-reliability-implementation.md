# Надёжная проверка кандидатов и дедлайнов — план реализации

> **Для агентных исполнителей:** ОБЯЗАТЕЛЬНЫЙ ПОДНАВЫК: используйте `superpowers:subagent-driven-development` (рекомендуется) или `superpowers:executing-plans`. Все задачи выполняются через TDD.

**Цель:** сделать поиск зависимым от локальной даты, сохранять результат проверки кандидата, надёжно переносить дедлайны в базу и показывать даты, ссылки и релевантность в Telegram.

**Архитектура:** единый `ApplicationClock` передаётся всем компонентам, которым нужна календарная дата. Глубокая проверка создаёт долговечный структурированный снимок кандидата; подтверждённый импорт использует этот снимок без второго вызова модели. Ручной дедлайн оформляется отдельным owner-scoped MCP proposal и сохраняется как предварительный до официальной проверки.

**Стек:** Python 3.12, aiogram 3, SQLAlchemy 2, Alembic, Pydantic 2, MCP, pytest, Ruff, SQLite, Docker Compose.

**Спецификация:** `docs/superpowers/specs/2026-10-19-candidate-deadline-reliability-design.md`

## Общие ограничения

- Все пользовательские сообщения и документация пишутся по-русски.
- Основная временная зона задаётся `APP_TIMEZONE`; значение примера — `Europe/Moscow`.
- В БД не попадают утверждения без источника, статуса и квитанции сохранения.
- Модель не исполняет и не генерирует исполняемый парсерный код.
- Мутации выполняются только после Telegram-подтверждения владельца.
- Существующая SQLite-база обновляется без потери данных.
- Работа выполняется в существующей ветке `main` по указанию владельца. Из-за ранее изменённого рабочего дерева автоматические промежуточные коммиты кода не создаются: каждый контрольный рубеж фиксируется тестами и `git diff --check`.

## Особое внимание при проверке

1. Граница суток: UTC ещё 18 октября, а в Москве уже 19 октября — поиск использует 19 октября.
2. Старый кандидат без JSON-снимка — карточка открывается, а первая проверка создаёт снимок.
3. Повреждённый или неполный снимок — импорт не падает и не сохраняет неподтверждённые данные.
4. Несколько конференций с похожим названием — ручной дедлайн не выбирает ID автоматически.
5. Недоступная дочерняя страница — факты основной страницы сохраняются, а ошибка отражается в примечаниях.

---

### Задача 1. Единые локальные часы процесса

**Файлы:**
- Создать: `src/conference_scout/runtime_clock.py`
- Изменить: `src/conference_scout/app.py`
- Изменить: `src/conference_scout/agent/prompts.py`
- Изменить: `src/conference_scout/agent/runtime.py`
- Изменить: `src/conference_scout/bot/usage_router.py`
- Тест: `tests/unit/test_runtime_clock.py`
- Тест: `tests/unit/bot/test_usage_router.py`
- Тест: `tests/unit/agent/test_runtime.py`

**Интерфейсы:**
- Создаёт: `ApplicationClock.start(timezone: ZoneInfo, now_provider=...) -> ApplicationClock`.
- Создаёт: `ApplicationClock.now() -> datetime`, `ApplicationClock.today() -> date`, `ApplicationClock.started_at: datetime`.
- Передаёт: dependency `application_clock` в aiogram middleware и экземпляр часов в `AgentRuntime`.

- [ ] Написать тест границы суток для `Europe/Moscow`: `2026-10-18 21:30 UTC` должно давать локальную дату `2026-10-19`.
- [ ] Запустить `pytest tests/unit/test_runtime_clock.py -q` и получить ожидаемый FAIL из-за отсутствия `ApplicationClock`.
- [ ] Реализовать `ApplicationClock` с внедряемым источником времени и неизменяемым `started_at`.
- [ ] Написать тест форматтера настроек на временную зону, время запуска, текущее время и дату начала поиска.
- [ ] Изменить `format_usage_snapshot`/`show_usage`, чтобы принимать часы и выводить новые поля.
- [ ] Написать тест системного промпта: он содержит локальную дату и временную зону, но не содержит секретов.
- [ ] Передать часы из `run_application` в `_run_bot`, `AgentRuntime`, read-handlers и сервисы.
- [ ] Заменить относящиеся к пользовательской календарной дате вызовы `date.today()` в `app.py` и `conference_router.py` на `clock.today()`.
- [ ] Запустить целевые тесты задачи и `ruff check` для изменённых файлов.

### Задача 2. Миграция и долговечный снимок кандидата

**Файлы:**
- Создать: `migrations/versions/20261019_0010_candidate_research_snapshot.py`
- Изменить: `src/conference_scout/db/models.py`
- Изменить: `src/conference_scout/db/repositories/conference_research.py`
- Тест: `tests/integration/test_migrations.py`
- Тест: `tests/integration/test_candidate_research.py`

**Интерфейсы:**
- Добавляет nullable-поля `EventCandidate.start_date`, `end_date`, `location`, `official_url`, `research_payload`, `researched_at`, `topic_relevance_score`, `topic_relevance_reason`.
- Создаёт: `ConferenceResearchRepository.save_candidate_snapshot(user_id, candidate_id, research, relevance, researched_at) -> EventCandidate`.
- Создаёт: `ConferenceResearchRepository.load_candidate_snapshot(user_id, candidate_id) -> VerifiedConferenceResearch | None`.

- [ ] Дополнить migration-тест: обновление с `20261016_0009` сохраняет кандидатов и создаёт новые nullable-колонки.
- [ ] Запустить migration-тест и подтвердить FAIL на отсутствующих колонках.
- [ ] Реализовать миграцию `0010` с корректными `upgrade` и `downgrade` для SQLite batch mode.
- [ ] Добавить поля в ORM-модель.
- [ ] Написать integration-тест round-trip снимка с датами, ориентировочным дедлайном, местом и релевантностью.
- [ ] Реализовать сохранение через `model_dump(mode="json")` и безопасную загрузку через `VerifiedConferenceResearch.model_validate`.
- [ ] При ошибке валидации возвращать `None`, не удаляя исходную запись и не используя повреждённый JSON.
- [ ] Запустить migration/repository тесты и Ruff.

### Задача 3. Политика даты и тематической релевантности

**Файлы:**
- Создать: `src/conference_scout/conferences/candidate_policy.py`
- Изменить: `src/conference_scout/conferences/research.py`
- Изменить: `src/conference_scout/ai/prompts.py`
- Изменить: `src/conference_scout/ai/service.py`
- Изменить: `src/conference_scout/schemas/topics.py`
- Тест: `tests/unit/conferences/test_candidate_policy.py`
- Тест: `tests/unit/ai/test_service.py`
- Тест: `tests/integration/test_candidate_research.py`

**Интерфейсы:**
- Создаёт: `TopicRelevanceContext(name: str, description: str, keywords: tuple[str, ...])`.
- Создаёт: `evaluate_keyword_relevance(title, summary, topics) -> KeywordRelevance`.
- Создаёт: `research_event_range(research) -> tuple[date | None, date | None]`.
- Создаёт: `is_expired_research(research, today: date) -> bool`.
- Расширяет analyzer: `rank_research_relevance(research, topics) -> RelevanceAssessment`.

- [ ] Написать unit-тесты нормализации русских/английских ключевых фраз, явного совпадения и пограничного нулевого совпадения.
- [ ] Написать unit-тесты истечения по `end_date`, по одиночной `start_date` и поведения при неизвестных датах.
- [ ] Запустить тесты и подтвердить ожидаемый FAIL.
- [ ] Реализовать чистые функции политики без доступа к БД и модели.
- [ ] Написать тест структурированного model relevance prompt/output.
- [ ] Добавить модельную оценку после извлечения страницы; темы загружать owner-scoped из включённых `Topic`.
- [ ] Явно нерелевантный поисковый результат не сохранять; пограничный результат сохранять для глубокой проверки.
- [ ] При глубокой оценке ниже `MIN_RELEVANCE_SCORE` ставить `SKIPPED`; при завершившемся событии ставить `EXPIRED`.
- [ ] В поисковые запросы/контекст добавить текущую локальную дату.
- [ ] Запустить целевые unit/integration тесты и Ruff.

### Задача 4. Проверка один раз, повторное использование снимка и дочерние страницы

**Файлы:**
- Изменить: `src/conference_scout/conferences/research.py`
- Изменить: `src/conference_scout/conferences/persistence.py`
- Изменить: `src/conference_scout/app.py`
- Тест: `tests/integration/test_candidate_research.py`
- Тест: `tests/integration/test_conference_persistence.py`

**Интерфейсы:**
- `ConferenceResearchService.inspect` сохраняет снимок только после evidence validation, date policy и relevance policy.
- `ConferenceResearchService.add` сначала использует валидный снимок и вызывает исследование только при его отсутствии.
- `_relevant_research_links` возвращает максимум три основные и одну резервную same-domain ссылку.

- [ ] Написать regression-тест: `inspect` вызывает analyzer один раз, а последующий `add` не вызывает fetcher/analyzer повторно.
- [ ] Написать тест: ориентировочный и точный дедлайны снимка после `add` присутствуют в таблице `deadlines`.
- [ ] Написать тест: ошибка сохранения откатывает конференцию, дедлайны, evidence и статус кандидата.
- [ ] Запустить тесты и подтвердить FAIL текущего повторного исследования.
- [ ] Сохранять снимок в конце успешного `inspect` одной транзакцией с обновлением полей preview.
- [ ] Изменить `add`: загружать снимок, повторно применять date policy, затем передавать его в `ConferencePersistenceService.persist`.
- [ ] Переводить кандидата в `IMPORTED` только после committed receipt.
- [ ] Дополнить ранжирование ссылок названием события, датами, регистрацией, abstract/CFP, программой, местом и русскими терминами.
- [ ] Добавить резервную same-domain ссылку только если основные страницы не дали дат или дедлайнов.
- [ ] Проверить тестами, что недоступная дочерняя страница добавляет verifier note, но не стирает данные основной страницы.
- [ ] Запустить целевые тесты и Ruff.

### Задача 5. Карточки кандидатов и отслеживаемых конференций

**Файлы:**
- Изменить: `src/conference_scout/bot/conference_router.py`
- Изменить: `src/conference_scout/bot/keyboards.py`
- Тест: `tests/unit/bot/test_conference_router.py`

**Интерфейсы:**
- `candidate_card(candidate) -> str` выводит диапазон дат, место, релевантность, дедлайны и URL из preview/snapshot.
- `conference_lists_text(tracked, candidates, archived) -> str` выводит даты и ссылку для каждой отслеживаемой записи.
- `conference_details_text` добавляет официальный URL и время последней проверки.

- [ ] Написать failing-тест карточки непроверенного кандидата с явными «не проверено/не найдено».
- [ ] Написать failing-тест проверенного кандидата с диапазоном дат, местом, score/reason, двумя дедлайнами и HTML-экранированным URL.
- [ ] Написать failing-тест списка отслеживаемых с датами и `website` fallback на `source_url`.
- [ ] Реализовать компактные форматтеры даты, диапазона и ссылки, не допуская сырой HTML-разметки из внешних данных.
- [ ] Сохранить последовательную выдачу одного кандидата и существующие кнопки «Проверить», «Подробнее», «Отклонить».
- [ ] Разделить ошибки проверки по безопасным кодам без вывода исключений и upstream body.
- [ ] Запустить `tests/unit/bot/test_conference_router.py` и Ruff.

### Задача 6. Подтверждаемая ручная запись дедлайна

**Файлы:**
- Изменить: `src/conference_scout/schemas/agent.py`
- Изменить: `src/conference_scout/mcp/tools.py`
- Изменить: `src/conference_scout/mcp/server.py`
- Изменить: `src/conference_scout/app.py`
- Изменить: `src/conference_scout/conferences/persistence.py`
- Тест: `tests/unit/agent/test_actions.py`
- Тест: `tests/unit/mcp/test_tools.py`
- Тест: `tests/integration/test_agent_application.py`
- Тест: `tests/integration/test_conference_persistence.py`

**Интерфейсы:**
- Создаёт `UpsertDeadlineAction` с discriminator `upsert_deadline`.
- Валидирует: ровно одна форма даты — `exact_date` либо `approximate_year` с optional `approximate_month`.
- Создаёт MCP tool `find_conferences(query, limit)` и proposal `propose_upsert_deadline(...)`.
- Создаёт executor, возвращающий committed persistence receipt.

- [ ] Написать schema-тесты точной даты, ориентировочного месяца и запрещённой смешанной/пустой даты.
- [ ] Написать MCP-тест owner scope и того, что proposal только создаёт `PENDING`, а не меняет БД.
- [ ] Написать integration-тест неоднозначного поиска имени: возвращаются варианты, mutation не создаётся.
- [ ] Написать integration-тест подтверждения ориентировочного дедлайна IMBASE и идемпотентного повторного выполнения.
- [ ] Запустить тесты и подтвердить FAIL из-за отсутствующего действия/инструмента.
- [ ] Добавить action в `ActionPayload` и parser, MCP allowlist, server и read handlers.
- [ ] Реализовать owner-scoped поиск по нормализованному имени среди конференций из тем и подписок.
- [ ] Реализовать executor: проверить доступ, выбрать source URL, создать `DeadlineClaim` со статусом `TENTATIVE` и сохранить через транзакционный persistence service.
- [ ] Возвращать квитанцию с типом и фактической датой/месяцем; запрещать текст «добавлено» без committed receipt.
- [ ] Запустить целевые тесты и Ruff.

### Задача 7. Выдача дедлайнов, статус TRACKING и инструкции агента

**Файлы:**
- Изменить: `src/conference_scout/app.py`
- Изменить: `src/conference_scout/db/repositories/reminders.py`
- Изменить: `src/conference_scout/agent/prompts.py`
- Изменить: `src/conference_scout/mcp/server.py`
- Тест: `tests/unit/db/test_repositories.py`
- Тест: `tests/unit/mcp/test_tools.py`
- Тест: `tests/unit/agent/test_runtime.py`

**Интерфейсы:**
- `list_upcoming_deadlines` возвращает точные и ориентировочные будущие записи, отсортированные относительно `ApplicationClock.today()`.
- `list_conferences(status="TRACKING")` фильтрует по owner subscription, а не только по `Conference.status`.
- Системный prompt предписывает `find_conferences` → `propose_upsert_deadline` и запрещает выбирать неоднозначное совпадение.

- [ ] Написать regression-тест MCP-списка конференций: подписанная конференция со статусом строки `DISCOVERED` видна при фильтре `TRACKING`.
- [ ] Написать тест будущего ориентировочного дедлайна и исключения прошедшего месяца.
- [ ] Написать runtime-тест запроса «установи дедлайн IMBASE»: модель получает доступные инструменты и предложение превращается в кнопку.
- [ ] Исправить read handlers, используя subscription status и `ReminderRepository.upcoming` с clock date.
- [ ] Обновить описания MCP-инструментов и системный промпт.
- [ ] Запустить целевые тесты и Ruff.

### Задача 8. Документация, регрессия и контейнер

**Файлы:**
- Изменить: `README.md`
- Изменить: `.env.example`
- Изменить: `tests/unit/test_documentation.py`
- Изменить: `tests/unit/test_deployment_files.py`

**Интерфейсы:**
- Документирует `APP_TIMEZONE`, локальную дату поиска, снимки кандидатов, статусы ручных дедлайнов и двухступенчатую релевантность.

- [ ] Сначала дополнить documentation-тесты ожидаемыми русскими разделами и значением `APP_TIMEZONE=Europe/Moscow`.
- [ ] Обновить README и `.env.example` без реальных ключей.
- [ ] Запустить новые и затронутые тесты.
- [ ] Запустить полный `.venv/bin/pytest -q`; ожидается отсутствие failures.
- [ ] Запустить `.venv/bin/ruff check --no-cache .`; ожидается `All checks passed!`.
- [ ] Запустить `git diff --check` и `docker compose config --quiet`.
- [ ] Собрать `docker compose build confplanner`.
- [ ] Выполнить import smoke внутри `confplanner:latest`.
- [ ] Проверить миграцию на копии существующей БД и наличие прежних конференций, подписок и дедлайнов.
