# Глубокий поиск: браузер и возобновляемая очередь

## Реализовано
- `browser-worker` на Playwright Chromium; не имеет опубликованного наружу порта.
- `PageFetcher` сначала делает обычный HTTP-запрос. Если ответ похож на пустую JavaScript-оболочку, обращается к `browser-worker` и извлекает DOM после выполнения JS. Коды 403/429 и CAPTCHA не обходятся.
- `discovery_url_tasks`: отдельные сохраняемые задачи `CATALOG` и `INSPECT`, уникальный `(job_id, normalized_url, task_type)`.
- Статусы `PENDING/RUNNING/DONE/FAILED`, `lease_until`, повторные попытки (до 3), возобновление истёкших `RUNNING` после перезапуска.
- Для оставшейся очереди поиск повторно ставится в `QUEUED`. Архивные `DONE`-задачи не исполняются снова; ранее найденные кандидаты остаются в SQLite.
- Существующая миграция Alembic дополнена `20261030_0021`.

## Развёртывание
Из распакованного архива копировать весь репозиторий с сохранением путей, включая `deploy/browser/`, `compose.yaml`, `migrations/versions/`, `src/`.

```bash
cd /mnt/data/ConfPlanner
# Резервная копия SQLite (предпочтительна sqlite3 .backup до изменения схемы):
docker exec confplanner python -c 'import sqlite3; s=sqlite3.connect("/data/conference_planner.db"); d=sqlite3.connect("/data/conference_planner.before-url-queue.db"); s.backup(d); d.close(); s.close()'
docker compose up -d --build --force-recreate browser-worker confplanner

docker compose ps
docker compose logs --tail=60 browser-worker confplanner
# Проверка новой миграции:
docker exec confplanner python -c 'import sqlite3; c=sqlite3.connect("/data/conference_planner.db"); print(c.execute("select version_num from alembic_version").fetchone()); print(c.execute("select name from sqlite_master where name=?",("discovery_url_tasks",)).fetchone())'
# Задания URL, актуальный прогресс:
docker exec confplanner python -c 'import sqlite3; c=sqlite3.connect("/data/conference_planner.db"); print(c.execute("select job_id,task_type,status,count(*) from discovery_url_tasks group by 1,2,3 order by 1 desc").fetchall())'
```

## Что осталось вне реализации
- Динамические страницы с содержимым после пользовательских действий (кнопки фильтров, бесконечная прокрутка) могут потребовать специализированных рецептов.
- Проверка PDF/DOC/DOCX, детальный анализ тезисов и формальное ранжирование научных программ — отдельные компоненты.
- Для нескольких одновременно работающих Docker-реплик SQLite не является оптимальным координатором; нынешняя очередь рассчитана на один экземпляр воркера ConfPlanner.
- Сетевую изоляцию Chromium на уровне хоста (ограничение исходящих обращений к внутренним адресам, даже при DNS rebinding) следует дополнительно обеспечить firewall-правилами Docker/хоста. Проверка IP в приложении не заменяет сетевую изоляцию.

## Эксплуатационные замечания
- Chromium запускается только при подозрении на client-rendered страницу, а не для каждого URL.
- Каталог сначала сохраняет найденные ссылки и результат, затем переводится в `DONE`. При аварийном завершении повторная обработка не создаёт дубликатов благодаря уникальным ключам.
- Лимиты времени/количества открываемых страниц действуют на один проход, а не на всю очередь; при наличии невыполненных URL работа возобновляется.
