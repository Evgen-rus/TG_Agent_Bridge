# Рик: что делать, когда сломалось

Случайный доступ при сбое: диагностика → статус и журнал → поиск по событию.

## Три источника информации

| Источник | Команда | Что показывает |
| --- | --- | --- |
| Сводка | `sudo -u rick -H .venv/bin/python scripts/diagnose.py` | Итог `OVERALL`, очереди, heartbeat, сбои |
| Журнал сервиса | `journalctl -u rick.service --since '1 hour ago' --no-pager` | Старт, рестарт, ошибки, трейсбеки |
| Лог приложения | `runtime/logs/agentbridge.log` | Цепочка событий по обработке |

Все команды выполнять **из корня проекта** — `/home/rick/TG_Agent_Bridge`.

- Время в `journalctl` — **московское**, в `agentbridge.log` — **UTC**. Сверяйте
  по дате, иначе легко искать не там.
- Журнал systemd хранится 14 дней, лог приложения — 7.
- `UNKNOWN` в диагностике значит «нет живого подтверждения», а не «поломалось».
- **Никогда** не печатайте `.env`, `~/.codex/auth.json`, переписку клиентов
  и сырые промпты модели.

## Порядок действий

```bash
cd /home/rick/TG_Agent_Bridge
sudo -u rick -H .venv/bin/python scripts/diagnose.py
systemctl status rick.service --no-pager
journalctl -u rick.service --since '1 hour ago' --no-pager
```

Дальше искать по меткам `event=`, `chat_id`, `update_id`, ID рекомендаций и
доставок. Перезапускать сервис **только после** того, как найдена причина, и
только с разрешения владельца.

## Таблица симптомов

| Симптом | Где смотреть | Что делать |
| --- | --- | --- |
| Рик молчит | `service.status`, `app.run_status`, `telegram.poll_last_log`, очереди, `recent_failure` | Сначала причина, потом рестарт: `sudo systemctl restart rick.service` |
| Поллинг встал | `telegram_poll_stalled`, `telegram_poll_restart_success`, `telegram_polling_fatal` | Проверить сеть, токен, второй процесс-поллер. Сторож сам перезапустит зависший updater, systemd — упавший процесс |
| **Codex падает** | `codex.turn_failures_in_logs`, `codex.active_failures`, `codex.last_failure` | См. раздел «Codex и лимиты» ниже — самая частая причина |
| SQLite повреждена | `sqlite.health`, `disk.free_bytes`, владелец файла | Остановить сервис, **сохранить копию базы** до ремонта. Восстановление — только из проверенного снимка и с разрешения владельца |
| Мало места | `disk.free_bytes`, размеры `runtime/media` и логов | Убрать старые медиа и снимки. Базу и документы клиентов не удалять |
| Копится неотправленное | `inbox`, `recommendations`, `deliveries`, `reminders` | Проследить один ID по базе и журналу. Доставка в Telegram «хотя бы один раз»: после сбоя дубликат возможен |
| Бесконечный рестарт | `systemctl status`, `journalctl`, `startup_failed`, `process_failed` | Ошибка зависимостей, конфигурации или прав. `StartLimitBurst=5` не даёт зациклиться |
| Нет утреннего отчёта | `daily_reports`, `owner_query_deliveries`, часовой пояс | Создание отчёта и доставка в Telegram независимы: отчёт может создаться и не отправиться |
| Нет уведомления о сбое | `operational_state`, `previous_run_unclean` | Мягкая остановка (SIGTERM) уведомления не создаёт. Резкая — создаст после восстановления |
| Рик не может перезапуститься сам | `self_restarts`, `self_restart_launch_failed`, `/etc/sudoers.d/rick-restart` | Проверить, что правило sudoers на месте и разрешает только рестарт своего сервиса |

## Codex и лимиты

Самая частая причина «Рик отвечает, но не по делу» — **исчерпан лимит Codex**.
Бот продолжает работать: принимает сообщения, распознаёт голос, отвечает
технически, но вместо ответа по существу присылает заглушку вроде «Уточните, о
каком чате речь».

Признаки в диагностике:

```
codex.turn_failures_in_logs=1 codex.active_failures=1 codex.last_failure=2026-09-27T07:09:11+00:00
recent_failure 2026-09-27T07:09:11+00:00 event=codex_turn_failed
OVERALL: DEGRADED codex_turn_failures
```

`active_failures` — падения **после** последнего успеха. Когда появится хоть один
`codex.last_success`, старые сбои перестанут держать `DEGRADED` сами собой.

Где искать настоящую причину — смотрите в двух местах:

```bash
# 1. В журнале сервиса: поле reason= содержит текст ошибки
journalctl -u rick.service --since '1 hour ago' --no-pager | grep codex_turn

# 2. В логах самого Codex — там лежит код ошибки и время сброса лимита
grep -ho '"codex_error_info[^,}]*' ~/.codex/sessions/YYYY/MM/DD/*.jsonl | sort -u
grep -ho 'try again at [0-9:]* [AP]M' ~/.codex/sessions/YYYY/MM/DD/*.jsonl | sort -u
```

Что бывает:

- `usage_limit_exceeded` — лимит подписки. Время сброса пишет сам Codex
  (время указано в **UTC**). Лечится покупкой кредитов или ожиданием.
- `401` / ошибка авторизации — истёк `~/.codex/auth.json`. Войти заново, см.
  [README](../README.md#авторизация-codex).
- Ошибка вида «model is not supported ... with a ChatGPT account» — устарел
  пакет. Обновить `openai-codex` и `openai-codex-cli-bin`, см.
  [README](../README.md#обновление-codex).

**Голосовые при этом продолжают работать:** распознавание идёт по
`OPENAI_API_KEY`, а Codex — по подписке. Это разные счета, поэтому «голос
слышно, а ответ бессмысленный» — верный признак исчерпанного лимита Codex.

### Рик сообщает сам

При явной ошибке `usage_limit_exceeded` Рик отправляет владельцу одно
сообщение о лимите, а после первой успешной попытки — одно сообщение о
восстановлении:

```text
⚠️ Лимит Codex исчерпан. ... По данным Codex восстановление в 18:27 Novosibirsk (UTC+07:00).
✅ Codex снова отвечает — лимит восстановился.
```

Особенности, о которых важно знать:

- Уведомление приходит **при первом фактическом отказе**, а не само по себе.
  Пока Рик не обратился к модели, он не может знать о лимите. Тишина в чате
  лимит не выдаёт.
- Пока лимит активен, Рик **не задаёт уточняющих вопросов** и не обращается к
  модели: вместо «Уточните, о каком чате речь» он отвечает той же внятной
  причиной и прикладывает ваш вопрос. Заведомо отказных попыток не тратится.
- Повторные отказы **не плодят сообщения**: метка хранится в SQLite, поэтому
  десять неудач подряд дадут одно уведомление, и перезапуск процесса его не
  продублирует.
- Восстановление фиксирует **первая удачная попытка**, а не таймер: узнать
  время сброса иначе нельзя, Codex сообщает его только в тексте ошибки. Пока
  лимит активен, Рик не делает попыток сам — поэтому для проверки восстановления
  достаточно задать вопрос в чат.
- Время сброса Codex отдаёт в UTC, Рик показывает его **в зоне `OWNER_TIMEZONE`**
  (по умолчанию `Asia/Novosibirsk`) вместе со смещением. Смените `OWNER_TIMEZONE`
  в `.env` — подпись в сообщениях изменится сама.
- На другие ошибки (таймаут, `401`, `429`, неизвестная модель) уведомление
  **не приходит** — иначе лимит получал бы шум.

Проверить в базе, что уведомление ушло и доставлено:

```bash
cd /home/rick/TG_Agent_Bridge
sqlite3 runtime/agentbridge.sqlite3 \
  "SELECT id, owner_message_id, created_at, substr(text,1,60) FROM owner_query_deliveries ORDER BY id DESC LIMIT 5"
```

Расход можно снизить без правки кода: `OWNER_CODEX_REASONING_EFFORT=none`
в `.env`, затем `sudo systemctl restart rick.service`.

## Частые мелочи

**`codex.model=UNKNOWN` в диагностике — это нормально.** Скрипт принципиально
не читает `.env`. Реальная модель видна в журнале при запросе:
`codex_turn_started ... model=gpt-6-luna effort=xhigh`.

**`telegram.poll_last_log` отстаёт от `poll_success_at`.** Heartbeat пишется в
состояние базы, а в лог попадают только значимые события. Расхождение в минуты
— норма.

**`app.last_clean_stop=UNKNOWN` до первого корректного рестарта.** Поле
заполняется при мягкой остановке, помечается как `result=clean`.

**`git` ругается на `dubious ownership`.** Один раз:
`git config --global --add safe.directory /home/rick/TG_Agent_Bridge`.

**Ручной запуск падает с конфликтом.** Значит сервис уже работает — это
ожидаемо. Останавливайте через `systemctl`, а не второй процессом.

## После исправления

```bash
cd /home/rick/TG_Agent_Bridge
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q agentbridge tests
.venv/bin/python -m pip check
git diff --check
sudo -u rick -H .venv/bin/python scripts/diagnose.py
```

Треды Codex работают только на чтение. Не трогайте другие проекты на VPS, файлы
в `/etc` и учётные данные во время разбора. Запись в файлы для агента пока
невозможна: провайдер открыт в режиме `read_only`, и включать запись можно
только вместе с проверкой границ путей и своих тестов.
