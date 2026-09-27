# AgentBridge (Рик)

Телеграм-мост, который читает рабочие чаты и подсказывает владельцу, что делать.
Это **не автоответчик**: бот никогда не пишет в клиентские чаты, все сообщения
уходят только в чат владельца.

## Как это работает

```text
Рабочий чат в Телеграме
  → сообщения сохраняются в SQLite
  → за последние 20 секунд накапливается эпизод
  → Codex кратко оценивает ситуацию и предлагает ответ
  → формулировка прогоняется через стиль «Сепы»
  → предложение уходит в чат владельца
```

Дальше владелец нажимает **«Да, применить»** или **«Нет, уточнить»**, либо
пишет ответ своим словом. Всё решение остаётся за человеком.

Важные свойства:

- **История — в SQLite, а не в памяти агента.** База `runtime/agentbridge.sqlite3`
  и есть источник истины. Треды Codex — только связность между обращениями.
- **После перезапуска бэклог не теряется**, а обрабатывается пачками, без
  «залпов» из десятков старых рекомендаций.
- **У каждого чата свой тред Codex и свой контекст** — `config.yaml` и `wiki.md`
  разных чатов никогда не смешиваются.
- **Голосовые распознаются** через `gpt-4o-mini-transcribe` и передаются в
  Codex как обычный текст.
- **Утренний отчёт** владельцу в 07:30 по Москве.
- **При исчерпании лимита Codex Рик сообщает об этом сам** — одним сообщением
  в чат владельца, и ещё одним, когда лимит восстановится. Подробности в
  [Docs/OPERATIONS.md](Docs/OPERATIONS.md#рик-сообщает-сам).

## Как пользоваться ботом

В чате владельца (`OWNER_CHAT_ID`) бот отвечает, если:

- вы **ответили на сообщение бота**;
- вы **упомянули его** через `@spare_eyes_bot` или слово «Рик,» / «Агент,»
  в начале сообщения (в том числе в голосовом);
- вы написали **`Общий контекст: …`** — это глобальная память для всех чатов.

Для рабочих чатов бот молча накапливает эпизод. Для фразы без понятного адресата
(например «Рик, что с Татьяной?») бот покажет список подключённых чатов и
попросит уточнить.

Команды: `/rules` — активные правила, `/undo` — отключить последнее,
`/remind YYYY-MM-DD HH:MM текст` — напоминание, `/reminders` — очередь.

## Команды на VPS

Запускать **только из корня проекта** — там жёстко задан рабочий каталог.

```bash
cd /home/rick/TG_Agent_Bridge
```

### Старт, стоп, перезапуск

```bash
sudo systemctl start rick.service      # запустить
sudo systemctl stop rick.service       # остановить
sudo systemctl restart rick.service    # перезапустить
sudo systemctl enable rick.service     # автозапуск при загрузке ОС
systemctl status rick.service --no-pager
```

**Не запускайте второй процесс вручную**, пока сервис работает: Telegram
допускает только один получатель `getUpdates` на токен, и второй процесс упадёт.

```bash
cd /home/rick/TG_Agent_Bridge
.venv/bin/python -m agentbridge.main   # только при остановленном сервисе
```

### Логи

```bash
journalctl -u rick.service -f                      # смотреть вживую
journalctl -u rick.service --since '1 hour ago'    # за последний час
journalctl -u rick.service -p err --since today    # только ошибки
tail -f runtime/logs/agentbridge.log               # лог самого приложения
```

Время в `journalctl` — **московское**, в `agentbridge.log` — **UTC**. Сверяйте
по дате. В журнале systemd сообщения хранятся 14 дней, лог приложения — 7.

### Диагностика

```bash
sudo -u rick -H .venv/bin/python scripts/diagnose.py
```

Запускать **от пользователя `rick`** — иначе `codex.auth_file_present` покажет
`False` (у `root` свой домашний каталог). Скрипт ничего не меняет и не читает
`.env`. Итоговая строка `OVERALL` — главный показатель:

- `HEALTHY` — всё работает;
- `DEGRADED <причина>` — есть проблема, причина названа прямо в строке.

### Обновление кода

```bash
cd /home/rick/TG_Agent_Bridge
sudo systemctl stop rick.service
git pull
.venv/bin/python -m pip install -r requirements.txt   # если менялся requirements.txt
.venv/bin/python -m pytest -q
sudo systemctl start rick.service
.venv/bin/python scripts/diagnose.py                  # от rick, см. выше
```

Перед `git pull` имеет смысл сделать копию базы: `cp runtime/agentbridge.sqlite3 runtime/agentbridge.sqlite3.bak`.
Если что-то пошло не так — вернуть на место и запустить сервис заново.

### Обновление Codex

Актуальные версии сейчас: `openai-codex 0.157.1` и `openai-codex-cli-bin 0.157.1`.

```bash
cd /home/rick/TG_Agent_Bridge
sudo systemctl stop rick.service
.venv/bin/python -m pip install -U openai-codex openai-codex-cli-bin
.venv/bin/python -m pip list | grep -i codex     # проверить версии
sudo systemctl start rick.service
```

Останавливать сервис **не обязательно** для самой установки пакетов, но
**обязательно** перезапустите его после: процесс держит старый модуль в памяти.

Частая причина проблем — не этот проект, а устаревшая версия пакета:

```text
The 'gpt-6-luna' model is not supported when using Codex with a ChatGPT account.
```

Тогда обновляйте оба пакета. Проверить доступные версии:
`.venv/bin/python -m pip index versions openai-codex`.

### Авторизация Codex

Токен хранится в `~/.codex/auth.json` пользователя `rick`. Если файл пропал:

```bash
cd /home/rick/TG_Agent_Bridge
.venv/bin/python -c "from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())"
```

Путь из последней строки — готовый исполняемый файл. С ним:

```bash
<путь> login --device-auth
<путь> login status
```

Если доступа нет, заводите сессию вручную. **Не вставляйте токены и коды
авторизации в чат, логи или Git.**

## Настройки

Все настройки — в файле `.env` (права `600`, в Git не попадает). Полный список
с комментариями: `.env.example`. Значения по умолчанию:

| Переменная | По умолчанию | Зачем |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Токен бота, обязателен |
| `OWNER_CHAT_ID` | — | Чат владельца, куда идут все ответы |
| `OPENAI_API_KEY` | — | Распознавание голосовых и дневной отчёт |
| `CODEX_MODEL` | `gpt-6-luna` | Модель для рабочих чатов |
| `CODEX_REASONING_EFFORT` | `xhigh` | Глубина рассуждения |
| `OWNER_CODEX_MODEL` | `gpt-6-luna` | Модель для чата владельца |
| `OWNER_CODEX_REASONING_EFFORT` | `xhigh` | Глубина для чата владельца |
| `OWNER_TIMEZONE` | `Asia/Novosibirsk` | Часовой пояс фраз «сегодня», «за неделю» |
| `MESSAGE_BATCH_SECONDS` | `20` | Окно накопления эпизода |
| `DAILY_REPORT_TIME` | `07:30` | Время утреннего отчёта (Europe/Moscow) |
| `LOG_RETENTION_DAYS` | `7` | Сколько дней хранить логи |
| `TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | Модель распознавания голоса |

Снизить расход можно, не трогая код: `OWNER_CODEX_REASONING_EFFORT=none`
в `.env`, затем `sudo systemctl restart rick.service`.

## Для разработки

```bash
.venv/bin/python -m pytest -q                        # тесты (229 шт., ~30 с)
.venv/bin/python -m compileall -q agentbridge tests   # проверка синтаксиса
.venv/bin/python -m pip check                        # целостность зависимостей
git diff --check                                     # пробелы в diff
```

Архитектура и контракты — в `ARCHITECTURE.md`, правила работы для агента — в
`AGENTS.md`. Сейчас в рабочей копии изменены `agentbridge/agents/codex.py` и
`scripts/diagnose.py` (логирование причины сбоя Codex и вердикт `DEGRADED`).

## Дополнительно

- **[Установка с нуля](Docs/VPS_DEPLOY.md)** — если VPS пересоздали.
- **[Что делать, когда сломалось](Docs/OPERATIONS.md)** — таблица симптомов.
