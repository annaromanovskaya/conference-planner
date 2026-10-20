# План реализации единого еженедельного цикла конференций

> **Для агентного исполнителя:** ОБЯЗАТЕЛЬНЫЙ ПОДНАВЫК: использовать `superpowers:executing-plans` и выполнять план последовательно в текущей сессии (Native). Для прогресса используются флажки `- [ ]`.

**Цель:** Объединить ручной и понедельничный поиск, хранить скрытые решения по кандидатам, экономно перепроверять сайты и отправлять дедлайн-уведомления один раз в сутки.

**Архитектура:** SQLite хранит состояния и историю решений; `ConferenceCycleService` создаёт долговечный workflow из проверки отслеживаемых, перепроверки пропущенных и пагинированного поиска. Telegram и планировщик вызывают один сервис, а модели подключаются только после дедупликации, кеша и детерминированного парсинга.

**Технологии:** Python 3.12, aiogram 3, SQLAlchemy 2, Alembic, APScheduler, Pydantic 2, pytest, Ruff, SQLite, MCP.

**Спецификация:** `docs/superpowers/specs/2026-10-20-weekly-conference-cycle-design.md`

## Глобальные ограничения

- Все новые спецификации, планы и пользовательские сообщения пишутся на русском языке.
- SQLite остаётся источником истины; текстовая память LLM не используется для статусов кандидатов.
- `REJECTED` не открывает сайты и не вызывает модель без явной команды восстановления.
- Изменения состояния и сохранение конференции проверяют владельца; опасные изменения требуют подтверждения.
- Ручной и автоматический поиск используют один исполнитель и одинаковую дедупликацию.
- Нулевой результат поиска является успешным результатом, а не ошибкой.
- Модели не исполняют код, SQL или shell-команды.
- Существующие пользовательские изменения в рабочем дереве не перезаписываются.
- Каждая задача выполняется через цикл RED → GREEN → полный релевантный набор тестов.

## Фокус проверки

1. Один каталог с одинаковым URL и разными названиями событий должен сохранить разные карточки — покрыть в задаче 5.
2. Старый `SKIPPED` без достоверного происхождения не должен автоматически вернуться в очередь — покрыть миграционным тестом в задаче 2.
3. Ручной запуск во время понедельничного цикла не должен создать второй workflow — покрыть в задаче 6.
4. После простоя бот должен отправить одно актуальное напоминание, а не все пропущенные пороги — покрыть в задаче 8.
5. Полностью известные первые страницы поиска и ноль новых результатов должны завершаться успешно — покрыть в задачах 5 и 6.

---

### Задача 1: Краткий формат дедлайнов и корректный символ `&`

**Файлы:**
- Изменить: `src/conference_scout/bot/conference_router.py`
- Изменить: `src/conference_scout/bot/router.py`
- Тест: `tests/unit/bot/test_conference_router.py`
- Тест: `tests/unit/bot/test_router.py`

**Интерфейсы:**
- Использует: `deadline_list_text(values) -> str` и обработчик «Мои темы».
- Предоставляет: сгруппированный HTML без служебных пометок; безопасное HTML-отображение названий тем.

- [ ] **Шаг 1: Написать падающие тесты**

Добавить проверки:

```python
def test_deadline_list_hides_internal_approximate_flags():
    assert "ориентировочно" not in deadline_list_text(values)
    assert "предварительно" not in deadline_list_text(values)

async def test_topics_renders_ampersand_through_html():
    assert answer.kwargs["parse_mode"] == "HTML"
```

- [ ] **Шаг 2: Убедиться в RED**

Запустить: `uv run pytest -q tests/unit/bot/test_conference_router.py tests/unit/bot/test_router.py`

Ожидание: тесты падают из-за суффикса и отсутствующего `parse_mode`.

- [ ] **Шаг 3: Реализовать минимальное исправление**

В `deadline_list_text` оставить русскую дату, но не добавлять краткому списку признаки `tentative` и `approximate`. В обработчике тем сохранить `escape(item.name)` и добавить `parse_mode="HTML"`.

- [ ] **Шаг 4: Проверить GREEN**

Запустить команду шага 2; ожидание: PASS.

- [ ] **Шаг 5: Зафиксировать изменение**

```bash
git add src/conference_scout/bot/conference_router.py src/conference_scout/bot/router.py tests/unit/bot/test_conference_router.py tests/unit/bot/test_router.py
git commit -m "fix: улучши краткий вывод тем и дедлайнов"
```

### Задача 2: Состояния решений кандидата и безопасная миграция

**Файлы:**
- Изменить: `src/conference_scout/schemas/common.py`
- Изменить: `src/conference_scout/db/models.py`
- Изменить: `src/conference_scout/db/repositories/conference_research.py`
- Создать: `migrations/versions/20261020_0011_candidate_decisions.py`
- Тест: `tests/integration/test_migrations.py`
- Тест: `tests/unit/db/test_verified_monitoring_repository.py`

**Интерфейсы:**
- Предоставляет: `CandidateStatus.REJECTED`, `CandidateDecisionReason`, поля `decision_reason`, `decision_source`, `decision_at`, `next_review_at`.
- Предоставляет: `transition_candidate(user_id, candidate_id, status, reason, source, decided_at, next_review_at=None) -> EventCandidate` и `list_candidates_by_statuses(user_id, statuses, limit=100) -> list[EventCandidate]`.

- [ ] **Шаг 1: Написать падающие тесты модели, владельца и миграции**

Проверить переходы `ACTIVE → REJECTED`, `ACTIVE → SKIPPED`, восстановление `REJECTED → ACTIVE`, запрет доступа другого пользователя и миграцию старого `SKIPPED` в `LEGACY_SKIPPED` без автоматического восстановления.

- [ ] **Шаг 2: Убедиться в RED**

Запустить: `uv run pytest -q tests/unit/db/test_verified_monitoring_repository.py tests/integration/test_migrations.py`

- [ ] **Шаг 3: Добавить перечисления, поля и миграцию**

Миграция добавляет nullable-поля решения, обновляет совместимость enum/check-ограничений SQLite и не удаляет существующие строки. Неоднозначный старый `SKIPPED` получает `LEGACY_SKIPPED`.

- [ ] **Шаг 4: Реализовать методы репозитория**

Все выборки ограничить `user_id`; переход должен очищать `next_review_at` для `ACTIVE`, `IMPORTED`, `REJECTED` и `EXPIRED`.

- [ ] **Шаг 5: Проверить GREEN**

Запустить команду шага 2; ожидание: PASS.

- [ ] **Шаг 6: Зафиксировать изменение**

```bash
git add src/conference_scout/schemas/common.py src/conference_scout/db/models.py src/conference_scout/db/repositories/conference_research.py migrations/versions/20261020_0011_candidate_decisions.py tests/integration/test_migrations.py tests/unit/db/test_verified_monitoring_repository.py
git commit -m "feat: добавь память решений по кандидатам"
```

### Задача 3: Кнопки «Отслеживать», «Пропустить» и «Отклонить»

**Файлы:**
- Изменить: `src/conference_scout/conferences/research.py`
- Изменить: `src/conference_scout/bot/keyboards.py`
- Изменить: `src/conference_scout/bot/conference_router.py`
- Тест: `tests/integration/test_candidate_research.py`
- Тест: `tests/unit/bot/test_conference_router.py`

**Интерфейсы:**
- Использует: переходы задачи 2.
- Предоставляет: `defer(user_id, candidate_id)`, `reject(user_id, candidate_id)`, `restore(user_id, candidate_id)` и `prepare_for_tracking(user_id, candidate_id) -> EventCandidate`.

- [ ] **Шаг 1: Написать падающие тесты четырёх кнопок**

Проверить callback-данные `candidate:track:{id}`, `candidate:details:{id}`, `candidate:defer:{id}`, `candidate:reject:{id}` и немедленный показ следующего кандидата после пропуска/отклонения.

- [ ] **Шаг 2: Написать падающие интеграционные тесты переходов**

Проверить, что `prepare_for_tracking` переиспользует свежий снимок, проверяет кандидата без снимка и не создаёт подтверждение при непроверяемых данных.

- [ ] **Шаг 3: Убедиться в RED**

Запустить: `uv run pytest -q tests/unit/bot/test_conference_router.py tests/integration/test_candidate_research.py`

- [ ] **Шаг 4: Реализовать сервис и обработчики**

«Отслеживать» сначала вызывает `prepare_for_tracking`, затем создаёт существующее `ImportConferenceCandidateAction`. «Пропустить» и «Отклонить» записывают разные состояния и причины.

- [ ] **Шаг 5: Проверить GREEN и обратную совместимость подтверждений**

Запустить команду шага 3 и `uv run pytest -q tests/unit/bot/test_confirmation_router.py`.

- [ ] **Шаг 6: Зафиксировать изменение**

```bash
git add src/conference_scout/conferences/research.py src/conference_scout/bot/keyboards.py src/conference_scout/bot/conference_router.py tests/integration/test_candidate_research.py tests/unit/bot/test_conference_router.py
git commit -m "feat: раздели решения по кандидатам"
```

### Задача 4: Скрытые списки, подтверждаемые команды и каталог возможностей

**Файлы:**
- Создать: `src/conference_scout/agent/capabilities.py`
- Изменить: `src/conference_scout/agent/prompts.py`
- Изменить: `src/conference_scout/bot/agent_router.py`
- Изменить: `src/conference_scout/schemas/agent.py`
- Изменить: `src/conference_scout/mcp/tools.py`
- Изменить: `src/conference_scout/mcp/server.py`
- Изменить: `src/conference_scout/app.py`
- Тест: `tests/unit/agent/test_capabilities.py`
- Тест: `tests/unit/mcp/test_tools.py`
- Тест: `tests/integration/test_mcp_server.py`

**Интерфейсы:**
- Предоставляет: `capability_help_text() -> str`, `is_capability_question(text) -> bool`, read-tool `list_candidate_decisions(status, limit)` и proposal-tool `propose_candidate_decision(candidate_id, decision)`.
- Предоставляет: `SetCandidateDecisionAction` только для `ACTIVE`, `SKIPPED`, `REJECTED` с подтверждением владельца.

- [ ] **Шаг 1: Написать падающие тесты каталога**

Проверить, что `/help`, детерминированный ответ на «Что ты умеешь?»/«Какие возможности?» и системный prompt используют один каталог и упоминают понедельничный поиск, решения кандидатов и ежедневные напоминания.

- [ ] **Шаг 2: Написать падающие MCP-тесты**

Проверить скрытое чтение `REJECTED`/`SKIPPED`, лимиты, owner scope, отклонение неизвестного статуса и создание подтверждения без прямой записи.

- [ ] **Шаг 3: Убедиться в RED**

Запустить: `uv run pytest -q tests/unit/agent/test_capabilities.py tests/unit/mcp/test_tools.py tests/integration/test_mcp_server.py`

- [ ] **Шаг 4: Реализовать каталог, инструменты и исполнитель действия**

Обычное меню не менять. До вызова LLM распознавать только явные вопросы о возможностях (`is_capability_question`) и отвечать тем же форматтером, который использует `/help`; каталог также включить в системный prompt, чтобы ответы на составные вопросы не расходились с реальными инструментами.

- [ ] **Шаг 5: Проверить GREEN**

Запустить команду шага 3 и `uv run pytest -q tests/unit/agent/test_actions.py tests/integration/test_agent_application.py`.

- [ ] **Шаг 6: Зафиксировать изменение**

```bash
git add src/conference_scout/agent/capabilities.py src/conference_scout/agent/prompts.py src/conference_scout/bot/agent_router.py src/conference_scout/schemas/agent.py src/conference_scout/mcp/tools.py src/conference_scout/mcp/server.py src/conference_scout/app.py tests/unit/agent/test_capabilities.py tests/unit/mcp/test_tools.py tests/integration/test_mcp_server.py tests/unit/agent/test_actions.py tests/integration/test_agent_application.py
git commit -m "feat: добавь скрытую память решений в инструменты агента"
```

### Задача 5: Пагинированный поиск новых кандидатов

**Файлы:**
- Изменить: `src/conference_scout/conferences/research.py`
- Изменить: `src/conference_scout/db/repositories/conference_research.py`
- Тест: `tests/integration/test_candidate_research.py`

**Интерфейсы:**
- Предоставляет: `search_fresh(user_id, query, *, page_limit, results_per_page) -> CandidateSearchReport`.
- `CandidateSearchReport` содержит `active_ids`, `new_ids`, `pages_checked`, `duplicates`, `exhausted`.

- [ ] **Шаг 1: Написать падающие тесты пагинации и дедупликации**

Проверить: известная первая страница ведёт ко второй; `REJECTED` не открывается; ноль новых результатов возвращает пустой успешный отчёт; два события одного каталога с разными названиями не объединяются.

- [ ] **Шаг 2: Убедиться в RED**

Запустить: `uv run pytest -q tests/integration/test_candidate_research.py -k "fresh or pagination or catalog"`.

- [ ] **Шаг 3: Реализовать `search_fresh`**

На каждой странице сначала создать/обновить безопасные карточки поисковой выдачи, исключить статусы `REJECTED` и `IMPORTED`, продолжать до первого набора активных новых записей либо исчерпания `page_limit`.

- [ ] **Шаг 4: Проверить GREEN**

Запустить команду шага 2 и весь `tests/integration/test_candidate_research.py`.

- [ ] **Шаг 5: Зафиксировать изменение**

```bash
git add src/conference_scout/conferences/research.py src/conference_scout/db/repositories/conference_research.py tests/integration/test_candidate_research.py
git commit -m "feat: продолжай поиск после известных результатов"
```

### Задача 6: Долговечный единый `ConferenceCycleService`

**Файлы:**
- Создать: `src/conference_scout/conferences/cycle.py`
- Изменить: `src/conference_scout/conferences/research.py`
- Изменить: `src/conference_scout/schemas/common.py`
- Изменить: `src/conference_scout/workflows/planner.py`
- Изменить: `src/conference_scout/workflows/engine.py`
- Изменить: `src/conference_scout/db/models.py`
- Изменить: `src/conference_scout/db/repositories/agent_workflows.py`
- Изменить: `src/conference_scout/app.py`
- Создать: `migrations/versions/20261020_0012_workflow_kind.py`
- Создать: `tests/integration/test_conference_cycle.py`
- Изменить: `tests/integration/test_candidate_research.py`
- Изменить: `tests/unit/workflows/test_engine.py`
- Изменить: `tests/integration/test_migrations.py`

**Интерфейсы:**
- Предоставляет: `CycleMode`, `CycleRequest`, `ConferenceCycleReport` и `ConferenceCycleService.run(user_id, request, progress=None) -> ConferenceCycleReport`.
- Workflow-задачи: `CHECK_TRACKED_CONFERENCES`, `RECHECK_SKIPPED_CANDIDATES`, `SEARCH_CANDIDATES`, `SYNTHESIZE_RESPONSE`.

- [ ] **Шаг 1: Написать падающие интеграционные тесты порядка**

Проверить строгий порядок: отслеживаемые → индекс решений/пропущенные → новый поиск; частичная ошибка не отменяет готовые результаты; ноль кандидатов завершает запуск со статусом `COMPLETED`.

- [ ] **Шаг 2: Написать падающий тест single-flight**

Два вызова одного пользователя должны вернуть один активный `run_id`; разные пользователи могут запускаться независимо.

- [ ] **Шаг 3: Убедиться в RED**

Запустить: `uv run pytest -q tests/integration/test_conference_cycle.py tests/unit/workflows/test_engine.py`.

- [ ] **Шаг 4: Добавить типы задач, миграцию и поиск активного запуска**

Добавить `workflow_kind="conference_cycle"` в создаваемый run без изменения поведения существующих исследовательских workflow. Миграция `20261020_0012` заполняет старым запускам значение `conference_research`; миграционный тест проверяет обновление существующей базы.

- [ ] **Шаг 5: Реализовать сервис поверх существующих MonitoringService и ConferenceResearchService**

Ручной режим передаёт пользовательский запрос и `force=True` для отслеживаемых. Плановый режим строит запросы по темам и локальным годам. Для `USER_DEFERRED` и `LOW_RELEVANCE` добавить `recheck_skipped(user_id, candidate_id)`: он передаёт `ETag`/`Last-Modified` из `WebDocumentCacheRepository`, не вызывает модель при `304` или прежнем хеше и возвращает кандидата в `ACTIVE` только после новых релевантных фактов. Каждый этап возвращает типизированный артефакт и безопасную статистику.

- [ ] **Шаг 6: Проверить GREEN и восстановление**

Запустить команду шага 3 и `uv run pytest -q tests/integration/test_workflow_recovery.py`.

- [ ] **Шаг 7: Зафиксировать изменение**

```bash
git add src/conference_scout/conferences/cycle.py src/conference_scout/conferences/research.py src/conference_scout/schemas/common.py src/conference_scout/workflows/planner.py src/conference_scout/workflows/engine.py src/conference_scout/db/models.py src/conference_scout/db/repositories/agent_workflows.py src/conference_scout/app.py migrations/versions/20261020_0012_workflow_kind.py tests/integration/test_conference_cycle.py tests/integration/test_candidate_research.py tests/unit/workflows/test_engine.py tests/integration/test_workflow_recovery.py tests/integration/test_migrations.py
git commit -m "feat: добавь единый долговечный цикл конференций"
```

### Задача 7: Ручной запуск, прогресс и понедельничное расписание

**Файлы:**
- Изменить: `src/conference_scout/bot/router.py`
- Изменить: `src/conference_scout/bot/agent_router.py`
- Изменить: `src/conference_scout/bot/dependencies.py`
- Изменить: `src/conference_scout/scheduler/setup.py`
- Изменить: `src/conference_scout/app.py`
- Изменить: `src/conference_scout/config.py`
- Тест: `tests/unit/bot/test_router.py`
- Тест: `tests/unit/scheduler/test_setup.py`
- Тест: `tests/integration/test_discovery.py`
- Тест: `tests/unit/test_config.py`

**Интерфейсы:**
- Использует: `ConferenceCycleService.run` задачи 6.
- Предоставляет: ручной запуск из `SearchFlow` и scheduler job `weekly-conference-cycle` каждый понедельник.

- [ ] **Шаг 1: Написать падающие тесты ручного маршрута**

Проверить передачу исходного запроса без искусственной LLM-инструкции, промежуточные события, итоговую сводку и сообщение об уже активном запуске.

- [ ] **Шаг 2: Написать падающие тесты scheduler**

Ожидать задания `weekly-conference-cycle` по понедельникам и `daily-deadline-reminders` раз в сутки; старые `weekly-discovery`, `daily-monitoring`, `hourly-reminders` отсутствуют.

- [ ] **Шаг 3: Убедиться в RED**

Запустить: `uv run pytest -q tests/unit/bot/test_router.py tests/unit/scheduler/test_setup.py tests/integration/test_discovery.py tests/unit/test_config.py`.

- [ ] **Шаг 4: Подключить общий сервис к Telegram и scheduler**

Ручной запуск использует существующий `TelegramWorkflowReporter`. Автоматический отправляет краткое начало только при длительной работе и обязательный итог.

- [ ] **Шаг 5: Проверить GREEN**

Запустить команду шага 3.

- [ ] **Шаг 6: Зафиксировать изменение**

```bash
git add src/conference_scout/bot/router.py src/conference_scout/bot/agent_router.py src/conference_scout/bot/dependencies.py src/conference_scout/scheduler/setup.py src/conference_scout/app.py src/conference_scout/config.py tests/unit/bot/test_router.py tests/unit/scheduler/test_setup.py tests/integration/test_discovery.py tests/unit/test_config.py
git commit -m "feat: запускай общий цикл по понедельникам и вручную"
```

### Задача 8: Ежедневные неповторяющиеся напоминания

**Файлы:**
- Изменить: `src/conference_scout/db/models.py`
- Изменить: `src/conference_scout/db/repositories/reminders.py`
- Изменить: `src/conference_scout/reminders/service.py`
- Изменить: `src/conference_scout/config.py`
- Изменить: `src/conference_scout/app.py`
- Создать: `migrations/versions/20261020_0013_daily_reminders.py`
- Тест: `tests/unit/reminders/test_service.py`
- Тест: `tests/unit/db/test_repositories.py`
- Тест: `tests/integration/test_migrations.py`

**Интерфейсы:**
- Предоставляет: `ReminderNotice` для точной или ориентировочной даты и `ReminderService.process_daily(user_id, today, sender) -> ReminderReport`.
- Устойчивый ключ доставки: `deadline_id + notification_kind + threshold`.

- [ ] **Шаг 1: Написать падающие тесты порогов**

Проверить `30, 14, 7, 3, 1, 0`, отсутствие повторов, одно актуальное сообщение после простоя и одно уведомление в первый день ориентировочного месяца.

- [ ] **Шаг 2: Написать падающий миграционный тест**

Старые `days_before` преобразуются в `notification_kind="EXACT"` и соответствующий ключ без потери `sent_at`.

- [ ] **Шаг 3: Убедиться в RED**

Запустить: `uv run pytest -q tests/unit/reminders/test_service.py tests/unit/db/test_repositories.py tests/integration/test_migrations.py`.

- [ ] **Шаг 4: Реализовать хранилище и ежедневный сервис**

Локализовать тип дедлайна и дату. Для точной даты после простоя выбирать ближайший неотправленный применимый порог; для месяца отправлять только `APPROXIMATE_MONTH_START`.

- [ ] **Шаг 5: Настроить значения по умолчанию**

`REMINDER_DAYS=30,14,7,3,1,0`; добавить `REMINDER_HOUR` с безопасным диапазоном `0..23`. Scheduler задачи 7 вызывает `process_daily` один раз в сутки.

- [ ] **Шаг 6: Проверить GREEN**

Запустить команду шага 3 и `uv run pytest -q tests/unit/scheduler/test_setup.py tests/unit/test_config.py`.

- [ ] **Шаг 7: Зафиксировать изменение**

```bash
git add src/conference_scout/db/models.py src/conference_scout/db/repositories/reminders.py src/conference_scout/reminders/service.py src/conference_scout/config.py src/conference_scout/app.py migrations/versions/20261020_0013_daily_reminders.py tests/unit/reminders/test_service.py tests/unit/db/test_repositories.py tests/integration/test_migrations.py tests/unit/scheduler/test_setup.py tests/unit/test_config.py
git commit -m "feat: отправляй дедлайн-напоминания раз в сутки"
```

### Задача 9: Настройки, документация и сквозная проверка

**Файлы:**
- Изменить: `src/conference_scout/bot/usage_router.py`
- Изменить: `.env.example`
- Изменить: `README.md`
- Изменить: `TASK.md`
- Тест: `tests/unit/bot/test_usage_router.py`
- Тест: `tests/unit/test_deployment_files.py`
- Тест: `tests/unit/test_documentation.py`

**Интерфейсы:**
- Использует: расписание и состояние workflow предыдущих задач.
- Предоставляет: отображение последнего и следующего понедельничного запуска в «Настройках» и актуальные инструкции развёртывания.

- [ ] **Шаг 1: Написать падающие тесты настроек и документации**

Проверить локальное время следующего понедельника, состояние последнего цикла, `REMINDER_HOUR` и отсутствие описания почасовых напоминаний.

- [ ] **Шаг 2: Убедиться в RED**

Запустить: `uv run pytest -q tests/unit/bot/test_usage_router.py tests/unit/test_deployment_files.py tests/unit/test_documentation.py`.

- [ ] **Шаг 3: Реализовать вывод и обновить документы**

Не показывать внутренние списки решений в настройках. Описать запросы агенту, четыре кнопки кандидата, понедельничный цикл и ежедневные пороги.

- [ ] **Шаг 4: Проверить GREEN**

Запустить команду шага 2.

- [ ] **Шаг 5: Выполнить полную проверку**

Запустить:

```bash
uv run pytest -q
uv run ruff check src tests
git diff --check
docker compose build confplanner
```

Ожидание: все тесты проходят, Ruff и diff-check чистые, образ собирается с кодом выхода 0.

- [ ] **Шаг 6: Зафиксировать завершение**

```bash
git add src/conference_scout/bot/usage_router.py .env.example README.md TASK.md tests/unit/bot/test_usage_router.py tests/unit/test_deployment_files.py tests/unit/test_documentation.py
git commit -m "docs: опиши еженедельный цикл и ежедневные напоминания"
```

## Итоговое ручное приёмочное испытание

1. Запустить бота с тестовой базой и выбрать модель через `/model`.
2. Добавить тему с символом `&` и убедиться, что «Мои темы» показывает его корректно.
3. Выполнить поиск, пропустить одного кандидата и отклонить другого.
4. Повторить поиск: отклонённый отсутствует, известная первая страница не вызывает `NO_CANDIDATES`, новый результат следующей страницы появляется.
5. Запросить отклонённые и пропущенные обычным сообщением; убедиться, что в меню отдельных списков нет.
6. Нажать «Отслеживать» и проверить цепочку: проверка → подтверждение → отслеживаемая конференция с дедлайнами.
7. Запустить цикл повторно и убедиться в отсутствии дублей.
8. Подставить локальную дату за три дня до тестового дедлайна и убедиться в одном уведомлении.
9. Проверить «Настройки», `/help` и вопрос «Что ты умеешь?».
