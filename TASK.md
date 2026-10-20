Нужно спроектировать и реализовать с нуля production-quality open-source проект **Conference Scout** — self-hosted Telegram-бота для поиска научных конференций, отслеживания актуальных дат и дедлайнов и отправки напоминаний.

Проект будет запускаться постоянно на Linux-сервере в Docker-контейнерах и впоследствии опубликован на GitHub.

Не делай прототип в одном файле. Нужен небольшой, но нормально структурированный и тестируемый проект.

# 1. Назначение

Conference Planner — персональный AI-assisted бот, который:

1. Позволяет пользователю задавать интересующие научные тематики.
2. Преобразует тематики в набор релевантных поисковых ключевых слов.
3. Раз в неделю ищет в интернете новые научные конференции, workshop, symposium и meeting.
4. Использует при поиске:

   * обычный web search;
   * глобальный список доверенных сайтов, заданных пользователем.
5. Определяет релевантность найденной конференции пользовательским темам.
6. Находит официальный сайт мероприятия.
7. Извлекает:

   * название;
   * аббревиатуру;
   * тип мероприятия;
   * место;
   * даты проведения;
   * дедлайн подачи тезисов;
   * дедлайн регистрации;
   * early registration deadline, если есть;
   * URL официального сайта;
   * URL страниц, подтверждающих конкретные даты.
8. Позволяет пользователю вручную добавить найденную конференцию в отслеживание.
9. Регулярно проверяет отслеживаемые конференции и обнаруживает изменения дедлайнов.
10. Напоминает о приближающихся дедлайнах.
11. Хранит историю изменений дат.
12. Не позволяет LLM самостоятельно придумывать конференции или даты без источника.

Главная ценность приложения — **не просто найти конференцию, а не пропустить важный дедлайн и обнаружить его изменение**.

# 2. Основной сценарий

Например пользователь добавляет темы:

* AI / Machine Learning in Astronomy
* Stellar Spectroscopy
* Interstellar Medium
* Chemically Peculiar Stars

Для темы:

`AI / Machine Learning in Astronomy`

система может получить нормализованные ключевые выражения:

* astroinformatics
* machine learning astronomy
* AI astrophysics
* deep learning astronomy
* machine learning stellar spectra

Для:

`Interstellar Medium`

например:

* interstellar medium
* ISM astrophysics
* molecular clouds
* interstellar matter

Нельзя жестко зашивать эти значения. Это только примеры.

LLM используется при создании/редактировании темы для генерации разумного набора поисковых выражений.

Далее обычный код генерирует поисковые запросы по ключевым выражениям, типам мероприятий и годам.

Например:

```text
astroinformatics 2027 conference
astroinformatics 2027 workshop
"machine learning astronomy" 2027 conference
"AI astrophysics" symposium 2027

"interstellar medium" 2027 conference
"interstellar medium" 2027 workshop
"molecular clouds" astrophysics 2027 conference
```

Для доверенных доменов дополнительно:

```text
site:eso.org astroinformatics 2027
site:iau.org "interstellar medium" 2027
```

Искать как минимум:

* текущий календарный год;
* следующий календарный год.

Архитектура должна позволять впоследствии расширить диапазон поиска.

# 3. Ключевой архитектурный принцип

Не реализовывать систему как бесконтрольно действующего autonomous agent.

Использовать детерминированный pipeline:

```text
User topics
    ↓
Normalized keywords
    ↓
Query generation
    ↓
Search
    ↓
Candidate URLs
    ↓
Page retrieval
    ↓
LLM extraction
    ↓
Source verification
    ↓
Deduplication
    ↓
Relevance ranking
    ↓
Database
```

LLM используется только там, где действительно нужна работа с естественным языком.

Не использовать LLM для:

* вычисления количества дней до дедлайна;
* scheduler;
* хранения состояния;
* принятия решения о том, наступило ли время напоминания;
* сравнения дат;
* формирования ID;
* хранения конференций;
* определения того, изменилось ли поле в БД.

Это должен делать обычный Python-код.

# 4. LLM

Использовать **OpenRouter API**.

API key и модель должны задаваться исключительно через environment variables:

```env
OPENROUTER_API_KEY=YOUR_OPENROUTER_API_KEY
OPENROUTER_MODEL=YOUR_MODEL
```

Предполагается использование бесплатных моделей OpenRouter.

Код не должен быть привязан к конкретной модели.

Создать абстракцию:

```python
class LLMProvider(Protocol):
    ...
```

и реализацию:

```text
OpenRouterLLMProvider
```

Нужно предусмотреть возможность позднее добавить другой provider без изменения бизнес-логики.

LLM применяется для:

1. Нормализации пользовательской темы.
2. Генерации поисковых keywords.
3. Анализа содержимого найденной страницы.
4. Structured extraction данных конференции.
5. Оценки тематической релевантности.
6. Возможно — semantic deduplication, только если обычных эвристик недостаточно.

Ответы LLM, которые должны попасть в БД, обязательно валидировать через **Pydantic v2**.

Никогда не сохранять сырой LLM JSON напрямую в БД.

Если structured output некорректен:

* выполнить ограниченный retry;
* залогировать ошибку;
* не портить существующие данные.

Не делать бесконечные retries.

# 5. Web search

Не использовать платный search API OpenRouter.

Развернуть отдельный контейнер **SearXNG** и использовать его как search backend.

Архитектура:

```text
Docker Compose
│
├── conference-scout
│
└── searxng
```

Conference Scout должен обращаться к SearXNG через внутреннюю Docker-сеть.

Создать абстракцию:

```python
class SearchProvider(Protocol):
    async def search(...) -> list[SearchResult]:
        ...
```

Реализация MVP:

```text
SearXNGSearchProvider
```

Бизнес-логика discovery не должна импортировать особенности SearXNG напрямую.

Результат поиска должен нормализоваться, например, до:

```python
class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str | None
    engine: str | None
```

Нужно:

* обрабатывать timeout;
* HTTP ошибки;
* пустые ответы;
* malformed response;
* временные ошибки поисковых backend;
* не падать целиком при ошибке одного запроса.

# 6. Trusted Sources

Пользователь может задать глобальный список источников, которым доверяет.

Источники относятся **ко всему боту**, а не к конкретной теме.

Поддержать:

```text
DOMAIN
```

например:

```text
eso.org
iau.org
```

и:

```text
PAGE
```

например:

```text
https://example.org/upcoming-conferences/
```

DOMAIN означает:

> при поиске дополнительно использовать запросы `site:domain ...`.

PAGE означает:

> регулярно проверять конкретную страницу как потенциальный каталог мероприятий.

Модель данных:

```text
trusted_sources
---------------
id
url
name
source_type       # domain | page
enabled
created_at
last_checked_at
```

Через Telegram пользователь должен иметь возможность:

* посмотреть список;
* добавить;
* отключить;
* включить;
* удалить источник.

# 7. Надежность источников

Достоверность источника и тематическая релевантность — разные характеристики.

Например ESO может быть надежным источником, но конференция по космологии может не соответствовать заданным темам.

Поэтому хранить отдельно:

```text
source confidence
```

и:

```text
relevance score
```

Приоритет источников примерно следующий:

1. официальный сайт самой конференции;
2. сайт организации-организатора;
3. пользовательский trusted source;
4. университет / обсерватория / научный институт;
5. научный агрегатор;
6. неизвестный сторонний сайт.

Не нужно пытаться создать идеальный универсальный classifier источников.

Главное правило:

**дедлайн не должен считаться подтвержденным только потому, что LLM его назвала.**

Дата должна иметь provenance.

Для каждой важной даты хранить:

```text
value
source_url
verified
last_checked_at
```

Если официальный источник найден:

```text
verified = true
```

Если дата найдена только на вторичном источнике:

```text
verified = false
```

Пользователю явно показывать это различие.

# 8. Conference extraction

После получения URL:

1. скачать страницу;
2. безопасно получить текстовое содержимое;
3. удалить очевидный мусор HTML;
4. ограничить передаваемый LLM размер текста;
5. передать LLM инструкцию извлечь только факты, присутствующие в странице;
6. валидировать результат Pydantic.

Пример модели:

```python
class ConferenceCandidate(BaseModel):
    name: str
    acronym: str | None
    event_type: str | None

    start_date: date | None
    end_date: date | None

    location: str | None

    abstract_deadline: date | None
    registration_deadline: date | None
    early_registration_deadline: date | None

    official_url: str | None

    source_url: str
```

Дополнительно хранить отдельные source URL для конкретных дедлайнов, если они обнаружены на других страницах.

Не заставлять модель угадывать отсутствующие данные.

Отсутствующее поле должно быть `null`.

# 9. Защита от hallucination

Это обязательное требование.

Нельзя делать:

```text
"Какие конференции будут по астроинформатике в 2027 году?"
```

и сохранять ответ модели как результат.

Всегда:

```text
Search engine
    ↓
real URL
    ↓
page content
    ↓
LLM extraction
```

LLM должна анализировать существующий источник.

Если название или дата не подтверждаются входным текстом — возвращать null / extraction failure.

# 10. Relevance

Для каждого кандидата оценивать соответствие пользовательским темам.

Можно использовать шкалу:

```text
0.0 ... 1.0
```

Например:

```text
0.90+
very relevant

0.70–0.89
relevant

0.50–0.69
possibly relevant

<0.50
normally not shown
```

Порог должен быть конфигурируемым.

LLM должна учитывать:

* название;
* описание;
* тематику конференции;
* пользовательские topics;
* normalized keywords.

Нельзя использовать relevance как показатель достоверности источника.

# 11. Deduplication

Одна конференция может встретиться:

* в Google-like search;
* через несколько search engine;
* на trusted source;
* через официальный сайт;
* на агрегаторе.

Нужно объединять такие результаты.

Использовать сначала детерминированные признаки:

* normalized name;
* acronym;
* URL/domain;
* event dates;
* близость названий.

Можно использовать fuzzy matching.

LLM использовать для semantic deduplication только в неоднозначных случаях.

Не объединять конференции только из-за одинаковой тематики.

# 12. Состояния конференции

Использовать как минимум:

```text
DISCOVERED
TRACKING
ARCHIVED
```

`DISCOVERED`

Конференция автоматически найдена, но пользователь ещё не решил, интересует ли она его.

`TRACKING`

Пользователь нажал «Отслеживать».

`ARCHIVED`

Конференция прошла или пользователь перестал её отслеживать.

Желательно использовать Enum.

# 13. Telegram UX

Использовать **aiogram 3.x**.

Telegram работает через long polling.

Внешние inbound HTTP ports для Telegram не нужны.

Доступ к боту в MVP должен иметь только владелец.

Environment variable:

```env
OWNER_TELEGRAM_ID=YOUR_TELEGRAM_ID
```

Если сообщение приходит от другого Telegram ID:

* не выполнять команды;
* не раскрывать конфигурацию;
* можно вернуть короткое сообщение о приватном режиме.

При этом архитектура БД должна уже содержать `user_id`, чтобы позднее можно было сделать приложение многопользовательским.

Главное меню примерно:

```text
🔭 Conference Scout

[ Мои темы ]
[ Найти конференции ]
[ Конференции ]
[ Ближайшие дедлайны ]
[ Источники ]
[ Настройки ]
```

Не обязательно буквально копировать оформление, главное сохранить функциональность.

## Мои темы

Показывает список:

```text
⭐ AI / Machine Learning in Astronomy
⭐ Interstellar Medium
⭐ Stellar Spectroscopy

[ + Добавить тему ]
```

Добавление темы:

пользователь вводит естественный текст:

```text
Применение машинного обучения к анализу звездных спектров
```

LLM генерирует:

* canonical name;
* short description;
* search keywords.

Показать пользователю результат перед сохранением.

## Найти конференции

Позволяет вручную запустить discovery вне weekly schedule.

После поиска:

```text
🔎 Найдено 5 новых конференций

Astroinformatics 2027
Релевантность: 94%

14–18 June 2027
Leiden

Abstract deadline:
12 February 2027
✅ подтверждено

[ Отслеживать ]
[ Подробнее ]
[ Неинтересно ]
```

## Конференции

Разделить хотя бы:

```text
Отслеживаемые
Найденные
Архив
```

## Ближайшие дедлайны

Сортировка по ближайшей дате.

Показывать количество дней.

Количество дней вычисляется Python-кодом, не LLM.

## Источники

Глобальный список trusted sources.

# 14. Feedback

Если пользователь нажимает:

```text
Неинтересно
```

не нужно сразу реализовывать сложную обучаемую recommendation system.

Но факт отказа необходимо сохранить.

Например:

```text
conference_feedback
-------------------
user_id
conference_id
decision
created_at
```

Это позволит позднее использовать историю для ranking.

# 15. Database

Использовать SQLite для MVP.

Рекомендуется SQLAlchemy 2.x + aiosqlite.

Не обращаться к SQLite произвольными SQL-строками по всему проекту.

Сделать repository/service layer.

Минимальные сущности:

## users

```text
id
telegram_id
created_at
```

## topics

```text
id
user_id
name
description
keywords
enabled
created_at
updated_at
```

keywords можно хранить как JSON.

## trusted_sources

```text
id
url
name
source_type
enabled
created_at
last_checked_at
```

## conferences

```text
id
name
normalized_name
acronym
event_type
website
start_date
end_date
location
status
created_at
updated_at
last_checked_at
```

## conference_topics

```text
conference_id
topic_id
relevance_score
```

## deadlines

```text
id
conference_id
deadline_type
deadline_date
source_url
verified
last_checked_at
created_at
updated_at
```

deadline_type должен быть Enum или ограниченным набором:

```text
ABSTRACT_SUBMISSION
REGISTRATION
EARLY_REGISTRATION
OTHER
```

## subscriptions

```text
id
user_id
conference_id
status
created_at
```

## reminders

```text
id
user_id
deadline_id
days_before
sent_at
created_at
```

## deadline_history

```text
id
conference_id
deadline_type
old_date
new_date
detected_at
source_url
```

## conference_feedback

```text
id
user_id
conference_id
decision
created_at
```

При необходимости нормализуй схему, если это делает реализацию лучше, но не усложняй её без причины.

# 16. Deadline history

Очень важная функция.

Например в БД было:

```text
Abstract submission:
2027-02-12
```

При очередной проверке официальный сайт сообщает:

```text
2027-02-19
```

Нужно:

1. обновить deadline;
2. сохранить history:

```text
old_date = 2027-02-12
new_date = 2027-02-19
```

3. отправить Telegram сообщение:

```text
🔄 Изменился дедлайн

Astroinformatics 2027

Подача тезисов:
12 февраля → 19 февраля

Источник:
<официальный URL>
```

# 17. Scheduler

Использовать APScheduler или аналогичную простую библиотеку.

Не использовать Celery/Redis для MVP.

Три основные группы задач.

## Weekly discovery

Каждый понедельник в `DISCOVERY_HOUR`:

```text
topics
  ↓
query generation
  ↓
SearXNG
  ↓
trusted source search
  ↓
candidate extraction
  ↓
verification
  ↓
deduplication
  ↓
ranking
  ↓
DB
  ↓
Telegram summary
```

Час запуска configurable. День фиксирован: понедельник.

## Conference monitoring

В составе единого понедельничного цикла и при ручном запуске.

Только для `TRACKING`.

Проверить:

* официальный сайт;
* известные deadline source URLs.

Если появились изменения — записать history и уведомить.

## Reminder processing

Один раз в сутки в `REMINDER_HOUR`.

LLM не используется.

Проверяет дедлайны и отправляет напоминания.

По умолчанию:

```text
30 дней
14 дней
7 дней
3 дня
1 день
0 дней
```

Сделать эти значения configurable.

Не отправлять одно и то же напоминание повторно.

# 18. HTTP page fetcher

Реализовать отдельный компонент.

Например:

```python
class PageFetcher:
    ...
```

Использовать httpx AsyncClient.

Требования:

* timeout;
* redirects;
* user-agent;
* maximum response size;
* только HTTP/HTTPS;
* обработка недоступных страниц;
* content type check;
* ограничение количества redirect;
* не падать на одной плохой странице.

Не реализовывать полноценный headless browser в MVP.

Если JavaScript-only страница не читается — корректно отметить failure.

Архитектура должна позволять впоследствии добавить browser-based fetcher.

# 19. SSRF / URL safety

Поскольку пользователь может добавлять URL trusted source и бот автоматически ходит по URL из поиска, добавить базовую защиту от SSRF.

Не разрешать запросы к:

* localhost;
* 127.0.0.0/8;
* ::1;
* private IPv4 ranges;
* link-local;
* metadata endpoints;
* Docker/internal infrastructure через произвольные URL пользователя.

При этом обращение приложения к конфигурированному SearXNG внутри Docker network является отдельной доверенной интеграцией и не должно блокироваться общей URL-fetch логикой.

URL из search results и trusted sources должны проходить validation.

# 20. Logging

Использовать стандартный logging.

Структурировать события хотя бы логически:

```text
discovery started
query generated
search completed
candidate found
candidate rejected
conference created
conference merged
monitoring started
deadline changed
reminder sent
LLM error
search error
page fetch error
```

Никогда не логировать:

* Telegram token;
* OpenRouter API key;
* полный `.env`.

# 21. Error handling

Ошибка одной конференции не должна останавливать weekly discovery.

Использовать изоляцию ошибок на уровне:

```text
query
candidate
conference monitoring
```

Для внешних API:

* timeout;
* limited retry;
* backoff;
* логирование.

Не делать бесконечных циклов retry.

# 22. Docker

Проект должен запускаться через:

```bash
docker compose up -d
```

Нужны:

```text
Dockerfile
compose.yaml
.dockerignore
.env.example
```

Контейнер Conference Scout:

* non-root user;
* `restart: unless-stopped`;
* read-only root filesystem;
* writable persistent `/data`;
* tmpfs `/tmp`;
* `no-new-privileges`;
* drop all Linux capabilities;
* не монтировать Docker socket;
* не использовать privileged;
* не использовать host network;
* не публиковать ненужные host ports.

SQLite:

```text
/data/conference_scout.db
```

хранить на persistent Docker volume.

Пример:

```yaml
services:

  conference-scout:
    ...
    env_file:
      - .env
    volumes:
      - conference_data:/data
    read_only: true
    tmpfs:
      - /tmp
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

  searxng:
    ...
```

SearXNG можно настроить минимально, достаточно для programmatic JSON search.

Не открывать SearXNG наружу, если в этом нет необходимости.

# 23. Healthcheck

Добавить Docker healthcheck для Conference Scout.

Не нужно проверять OpenRouter или внешний интернет как обязательный критерий здоровья контейнера.

Healthcheck должен прежде всего проверять:

* процесс приложения;
* доступ к SQLite;
* возможность прочитать/записать внутреннее состояние приложения, если это необходимо.

Временный outage OpenRouter или Telegram не должен заставлять Docker постоянно перезапускать приложение.

# 24. Configuration

Использовать pydantic-settings.

`.env.example` примерно:

```env
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
OWNER_TELEGRAM_ID=YOUR_TELEGRAM_ID

OPENROUTER_API_KEY=YOUR_OPENROUTER_API_KEY
OPENROUTER_MODEL=YOUR_MODEL

DATABASE_URL=sqlite+aiosqlite:////data/conference_scout.db

SEARXNG_URL=http://searxng:8080

DISCOVERY_HOUR=10

REMINDER_HOUR=9
REMINDER_DAYS=30,14,7,3,1,0

MIN_RELEVANCE_SCORE=0.5

LOG_LEVEL=INFO
```

Можно изменить имена переменных, если есть более чистое решение.

Обязательное требование:

никаких реальных API key или Telegram token в репозитории.

`.env` должен быть в `.gitignore`.

# 25. Project structure

Предпочтительная структура:

```text
conference-scout/
│
├── src/
│   └── conference_scout/
│       │
│       ├── bot/
│       │   ├── handlers/
│       │   ├── keyboards.py
│       │   └── messages.py
│       │
│       ├── ai/
│       │   ├── provider.py
│       │   ├── openrouter.py
│       │   ├── prompts.py
│       │   ├── extraction.py
│       │   └── ranking.py
│       │
│       ├── search/
│       │   ├── provider.py
│       │   └── searxng.py
│       │
│       ├── web/
│       │   ├── fetcher.py
│       │   ├── parser.py
│       │   └── url_safety.py
│       │
│       ├── conferences/
│       │   ├── discovery.py
│       │   ├── monitoring.py
│       │   ├── verification.py
│       │   └── deduplication.py
│       │
│       ├── topics/
│       │   └── service.py
│       │
│       ├── reminders/
│       │   └── service.py
│       │
│       ├── db/
│       │   ├── models.py
│       │   ├── repositories/
│       │   └── session.py
│       │
│       ├── scheduler/
│       │   ├── jobs.py
│       │   └── setup.py
│       │
│       ├── schemas/
│       │
│       ├── config.py
│       ├── healthcheck.py
│       └── main.py
│
├── tests/
│
├── Dockerfile
├── compose.yaml
├── .dockerignore
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
├── LICENSE
└── ...
```

Не следовать этой структуре механически, если есть более чистые границы модулей.

Не создавать десятки пустых файлов.

Главный принцип:

каждый модуль должен иметь одну понятную ответственность.

# 26. Dependencies

Предпочтительно:

```text
Python 3.12+
aiogram 3.x
httpx
pydantic 2.x
pydantic-settings
SQLAlchemy 2.x
aiosqlite
APScheduler
BeautifulSoup4 или selectolax
```

Для fuzzy matching можно использовать RapidFuzz.

Не использовать без необходимости:

* LangChain;
* LangGraph;
* CrewAI;
* AutoGen;
* Celery;
* Redis;
* Kafka;
* vector DB;
* RAG;
* Kubernetes.

YAGNI.

# 27. Async architecture

Поскольку:

* Telegram;
* OpenRouter;
* SearXNG;
* page fetching;

являются I/O-bound, основной application stack должен быть async.

Не смешивать хаотично sync и async API.

# 28. Prompts

LLM prompts хранить отдельно от application logic.

Например:

```text
ai/prompts.py
```

При extraction явно говорить модели:

* используй только предоставленный текст;
* не используй собственные знания о конференции;
* не придумывай отсутствующие даты;
* если информация отсутствует — null;
* дата должна быть взята из источника;
* отвечай строго структурировано.

При ranking объяснить, что score — релевантность пользовательской научной тематике, а не престиж и не достоверность сайта.

# 29. Conference series

Полноценную сущность ConferenceSeries пока можно не реализовывать.

Но структура не должна мешать добавить её позднее для серий вроде:

```text
Cool Stars
IAU Symposium
Physics of Magnetic Stars
Astroinformatics
```

Не усложняй MVP ради этого.

# 30. Testing

Использовать pytest.

Нужны unit tests как минимум для:

* query generation;
* date parsing/validation;
* URL safety;
* deduplication;
* reminder calculation;
* reminder duplicate protection;
* relevance handling;
* deadline update detection;
* deadline history creation;
* Pydantic validation LLM response;
* owner Telegram ID access control.

Для внешних сервисов использовать mocks/fakes.

Не делать unit tests зависимыми от реального OpenRouter, Telegram или SearXNG.

Также добавить несколько integration tests:

```text
fake search
    ↓
fake page
    ↓
fake LLM
    ↓
conference persisted
```

и:

```text
existing deadline
    ↓
new extracted deadline
    ↓
history record
    ↓
notification requested
```

# 31. Development quality

Использовать:

* type hints;
* ruff;
* pytest;
* современный `pyproject.toml`.

Можно использовать mypy/pyright, если это не создаст лишней сложности.

Не оставлять:

```text
TODO
pass
NotImplementedError
```

в ключевой функциональности MVP.

# 32. README

README должен быть пригоден для GitHub.

Нужны разделы:

```text
Conference Scout
Features
Architecture
How it works
Requirements
Quick start
Telegram bot creation
OpenRouter configuration
SearXNG
Configuration
Docker deployment
Trusted sources
Topics
Discovery
Deadline tracking
Backups
Security
Development
Testing
Limitations
Roadmap
License
```

README должен объяснять:

```bash
git clone ...
cp .env.example .env
# edit .env
docker compose up -d
```

Также объяснить backup SQLite volume/database.

# 33. Security notes

Отдельно документировать:

* API secrets находятся только в `.env`;
* Telegram access ограничен `OWNER_TELEGRAM_ID`;
* container non-root;
* read-only root filesystem;
* no Docker socket;
* SSRF protection;
* URLs являются недоверенными данными;
* HTML страниц считается недоверенным вводом;
* содержимое внешних страниц не должно интерпретироваться как системные инструкции для LLM.

Это важно:

страницы конференций являются **данными**, а не инструкциями.

LLM extraction prompt должен быть устойчив к тексту вроде:

```text
Ignore previous instructions...
```

в содержимом страницы.

Передаваемый webpage text помещать как явно обозначенные недоверенные данные.

# 34. MVP scope

MVP должен полностью реализовать:

```text
Telegram interface
topics
trusted sources
manual discovery
weekly discovery
SearXNG search
OpenRouter extraction
relevance ranking
conference persistence
conference tracking
deadline persistence
deadline verification metadata
daily monitoring
deadline change history
deadline reminders
Docker deployment
tests
README
```

Не реализовывать сейчас:

* frontend;
* web dashboard;
* mobile app;
* OAuth;
* полноценную multi-user регистрацию;
* платёжную систему;
* собственный search engine;
* embeddings;
* vector database;
* RAG;
* сложный recommendation ML;
* headless browser;
* Kubernetes.

# 35. Multi-user readiness

Сейчас приложение персональное.

Telegram доступ разрешён только:

```env
OWNER_TELEGRAM_ID
```

Но БД и service boundaries должны быть спроектированы так, чтобы впоследствии можно было разрешить несколько пользователей.

То есть не использовать по всему коду глобальную конструкцию вроде:

```python
CURRENT_USER = 1
```

Получать пользователя по Telegram ID и передавать `user_id` явно.

# 36. Example user journey

Первый запуск:

```text
/start
```

Бот:

```text
🔭 Conference Scout

Тем пока нет.

Добавьте научную тематику, по которой нужно искать конференции.

[ + Добавить тему ]
```

Пользователь:

```text
ИИ и машинное обучение в астрономии, особенно анализ спектров звезд
```

LLM:

```text
Название:
AI / Machine Learning in Astronomy

Описание:
Machine-learning methods applied to astronomical and astrophysical data,
with emphasis on stellar spectroscopy.

Search keywords:
- astroinformatics
- machine learning astronomy
- machine learning stellar spectroscopy
- AI astrophysics
- deep learning stellar spectra
```

Бот:

```text
Сохранить тему?

[ Сохранить ]
[ Изменить ]
[ Отмена ]
```

После сохранения пользователь запускает:

```text
Найти конференции
```

Система выполняет discovery.

Результат:

```text
🔎 Найдены новые конференции: 4
```

Карточка:

```text
Astroinformatics 2027

Релевантность: 94%

14–18 June 2027
Leiden, Netherlands

Abstract deadline:
12 February 2027
✅ подтверждено официальным источником

[ Отслеживать ]
[ Подробнее ]
[ Неинтересно ]
```

После `Отслеживать`:

```text
✅ Конференция добавлена в отслеживание.

Я буду проверять изменения дат и напоминать о дедлайнах.
```

За 14 дней:

```text
⏰ Через 14 дней заканчивается подача тезисов

Astroinformatics 2027

Deadline:
12 February 2027

[ Подробнее ]
```

При изменении:

```text
🔄 Изменился дедлайн

Astroinformatics 2027

Подача тезисов:
12 февраля → 19 февраля

Источник:
https://...
```

# 37. Implementation approach

Сначала изучи требования и составь короткий implementation plan.

После этого реализуй проект полностью.

Работай небольшими логическими шагами.

Перед завершением обязательно:

1. запусти formatter/linter;
2. запусти unit tests;
3. запусти integration tests;
4. проверь Docker build;
5. проверь `docker compose config`;
6. проверь отсутствие secrets в репозитории;
7. проверь `.gitignore`;
8. проверь, что README соответствует фактической реализации.

Не утверждай, что тесты проходят, пока реально их не выполнишь.

# 38. Definition of Done

Проект считается готовым, когда:

* `docker compose up -d` поднимает Conference Scout и SearXNG;
* Telegram bot запускается;
* посторонний Telegram ID не может пользоваться ботом;
* можно добавить тему;
* LLM генерирует keywords;
* можно добавить trusted source;
* можно вручную запустить discovery;
* поиск идет через SearXNG;
* реальные URL проходят через extraction;
* LLM не является самостоятельным источником конференций;
* найденные конференции сохраняются;
* дубли объединяются;
* можно добавить конференцию в TRACKING;
* scheduler автоматически запускает discovery;
* scheduler мониторит TRACKING conferences;
* изменение дедлайна сохраняется в history;
* пользователь получает notification об изменении;
* пользователь получает дедлайн-reminders;
* duplicate reminders не отправляются;
* SQLite переживает пересоздание контейнера;
* OpenRouter model задается env-переменной;
* реальные секреты отсутствуют в git;
* тесты проходят;
* Docker image собирается;
* README позволяет другому пользователю развернуть свой экземпляр приложения.

Если при реализации обнаружишь неоднозначность, выбери наиболее простое разумное решение, которое соответствует архитектуре выше, и явно задокументируй его в README. Не расширяй scope без необходимости.
