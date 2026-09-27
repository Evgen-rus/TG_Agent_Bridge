# Установка Рика на Ubuntu (с нуля)

Нужно, если VPS пересоздали или проект переносите на другую машину. Описывает
полный цикл: пользователь, Python, зависимости, настройки, авторизация,
системный сервис.

**Схема на сервере:** без Docker, чистый `systemd`, виртуальное окружение в
`.venv`, база SQLite, всё состояние в каталоге `runtime/`.

## Что должно быть на сервере

Ubuntu с исходящим доступом к `api.telegram.org` и `api.openai.com` по HTTPS,
свободное место под `runtime/media` и ежедневные снимки диска.

## Шаг 1. Системные пакеты

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip tzdata sudo
```

> На Ubuntu 22.04 системный Python — 3.10, а проекту нужен **3.12**.
> Системный Python менять не надо: 3.12 ставится отдельно, рядом (шаг 3).

## Шаг 2. Пользователь и код

```bash
sudo adduser rick
sudo -u rick git clone https://github.com/Evgen-rus/TG_Agent_Bridge.git /home/rick/TG_Agent_Bridge
```

Все дальнейшие команды выполняются **от `rick`** и **из `/home/rick/TG_Agent_Bridge`**.
Проект не запускается из другой директории — рабочий каталог задан жёстко.

## Шаг 3. Python 3.12 и виртуальное окружение

На машине, где уже есть Python 3.11+, шаг с `bootstrap-venv` можно пропустить и
создать `.venv` обычным `python3 -m venv .venv`. На Ubuntu 22.04 нужен полный
вариант:

```bash
cd /home/rick/TG_Agent_Bridge
python3 -m venv ~/.bootstrap-venv
~/.bootstrap-venv/bin/python -m pip install uv
~/.bootstrap-venv/bin/uv python install 3.12
~/.bootstrap-venv/bin/uv venv --seed --python 3.12 .venv
```

**Что здесь происходит.** `~/.bootstrap-venv` — отдельное, «одноразовое»
окружение только ради установки `uv`: системный Python 3.10 не умеет создать
окружение 3.12. Дальше `uv` ставит собственный Python 3.12 в
`~/.local/share/uv/python/` и создаёт основное `.venv`, который уже ссылается на
него:

```text
.venv/bin/python -> /home/rick/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12
```

Проверить, что всё на месте:

```bash
.venv/bin/python -V                 # Python 3.12.x
cat .venv/pyvenv.cfg                 # version_info = 3.12, uv = <версия>
```

## Шаг 4. Зависимости

```bash
cd /home/rick/TG_Agent_Bridge
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q tests
```

Из `requirements.txt` ключевые пакеты: `openai-codex` (SDK и встроенный CLI
Codex), `openai-codex-cli-bin` (тот самый исполняемый файл), `python-telegram-bot`,
`openai`, `PyYAML`, `python-dotenv`, `pytest`.

**Отдельного `pip install codex` не нужно** — CLI лежит внутри пакета.

## Шаг 5. Настройки

```bash
cd /home/rick/TG_Agent_Bridge
cp .env.example .env
chmod 600 .env
nano .env
```

Обязательно заполнить:

| Переменная | Что вписать |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Токен бота от BotFather |
| `OWNER_CHAT_ID` | ID чата владельца (отрицательный для группы) |
| `OPENAI_API_KEY` | Для распознавания голосовых и утреннего отчёта |

Остальное имеет рабочие значения по умолчанию — см. `.env.example`. Проверить,
что ключи заданы, можно не показывая значений:

```bash
cut -d= -f1 .env
```

`.env` и каталог `runtime/` в Git не попадают. Значения оттуда нельзя
показывать в чате, логах или коммитах.

## Шаг 6. Каталоги и рабочие данные

```bash
cd /home/rick/TG_Agent_Bridge
mkdir -p runtime/logs runtime/media
chmod 700 runtime runtime/logs runtime/media
```

Подключённые чаты лежат в `chats/<имя>/` — по два файла на чат:

- `config.yaml` — `telegram_chat_id`, название, при необходимости `memory_project`;
- `wiki.md` — устойчивый контекст этого чата.

Каталог `chats/*` в Git **не входит**, поэтому при переносе с другого сервера
его нужно перенести отдельно. То же касается связанных файлов знаний,
на которые ссылаются конфиги.

## Шаг 7. Перенос базы (если переносите существующего Рика)

Источник истины — `runtime/agentbridge.sqlite3`.

1. **На старом сервере остановить процесс** бота.
2. Скопировать `runtime/agentbridge.sqlite3` и нужные файлы из `runtime/media`.
3. На новом сервере положить базу на место и выставить владельца:

```bash
sudo chown -R rick:rick /home/rick/TG_Agent_Bridge/runtime
```

Копировать базу при работающем процессе нельзя: файл может оказаться
нецелостным. Если остановить невозможно — использовать штатный механизм
SQLite для резервных копий вместо простого копирования. Снимок базы перед
изменением схемы сделать стоит заранее.

## Шаг 8. Авторизация Codex

Выполнять **от пользователя `rick`** — сервис видит тот же домашний каталог и ту
же папку `~/.codex`.

```bash
cd /home/rick/TG_Agent_Bridge
.venv/bin/python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())'
```

Путь из вывода — встроенный исполняемый файл. С ним:

```bash
<путь> login --device-auth
<путь> login status
```

Если в настройках ChatGPT отключён вход по коду устройства — включите его.
Подробности в [официальной инструкции OpenAI](https://learn.chatgpt.com/docs/auth).

Готовый токен лежит в `~/.codex/auth.json` — права `600`, владелец `rick`. Если
переносите этот файл, проверьте права после копирования. **Код авторизации,
`auth.json` и токены нельзя публиковать.**

## Шаг 9. Системный сервис

```bash
sudo cp /home/rick/TG_Agent_Bridge/deploy/systemd/rick.service /etc/systemd/system/rick.service
sudo chown root:root /etc/systemd/system/rick.service
sudo systemctl daemon-reload
```

Проверьте, что в юните верные путь и пользователь:

```ini
[Service]
User=rick
Group=rick
WorkingDirectory=/home/rick/TG_Agent_Bridge
ExecStart=/home/rick/TG_Agent_Bridge/.venv/bin/python -m agentbridge.main
```

Право Рику перезапускать самого себя (по одной команде, без шелла):

```bash
sudo cp /home/rick/TG_Agent_Bridge/deploy/systemd/rick-sudoers.example /etc/sudoers.d/rick-restart
sudo chown root:root /etc/sudoers.d/rick-restart
sudo chmod 0440 /etc/sudoers.d/rick-restart
sudo visudo -cf /etc/sudoers.d/rick-restart
```

Правило разрешает **только** `/usr/bin/systemctl --no-block restart rick.service`.
Никаких других команд и сервисов. Расширять его нельзя.

## Шаг 10. Запуск

```bash
sudo systemctl enable --now rick.service
systemctl status rick.service --no-pager
journalctl -u rick.service -n 100 --no-pager
sudo -u rick -H /home/rick/TG_Agent_Bridge/.venv/bin/python /home/rick/TG_Agent_Bridge/scripts/diagnose.py
```

В журнале ожидаемо:

```text
event=process_started component=telegram
event=catchup_finished result=ok
```

В диагностике — `OVERALL: HEALTHY`.

**Не запускайте второй процесс вручную**, пока сервис работает: на один токен
Telegram допускается только один получатель `getUpdates`, и второй процесс упадёт.
Если старый бот ещё где-то работает — остановите его **до** старта.

## Шаг 11. Проверка

- бот появился в чате владельца и отвечает на `@spare_eyes_bot`;
- в диагностике `telegram.poll_status=RECENT`;
- `~/.codex/auth.json` на месте, тестовый запрос проходит;
- в рабочем чате бот предлагает вариант ответа, но **не пишет в сам чат**;
- утром в 07:30 МСК пришёл отчёт; в базе появилась запись в `daily_reports`.

Мягкий рестарт не должен создавать уведомление о сбое:

```bash
sudo systemctl restart rick.service
```

Резкое завершение процесса, наоборот, должно создать его после восстановления.

## Обновление и откат

Обычное обновление кода описано в [README](../README.md#обновление-кода).
Коротко: остановить сервис, сделать снимок базы, `git pull`, при изменении
`requirements.txt` — установка, тесты, запуск сервиса, диагностика.

```bash
cd /home/rick/TG_Agent_Bridge
sudo systemctl stop rick.service
cp runtime/agentbridge.sqlite3 runtime/agentbridge.sqlite3.bak
git pull && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
sudo systemctl start rick.service
```

`git pull` не затрагивает `.env`, `runtime/`, `chats/*` и авторизацию — всё это
в Git не входит.

Откат: остановить сервис, вернуть предыдущий коммит
(`git checkout <коммит>`), при изменении схемы — вернуть снимок базы, запустить
сервис, выполнить диагностику.

После перезагрузки VPS:

```bash
systemctl is-enabled rick.service
systemctl status rick.service
```

## Резервные копии

Отдельного встроенного планировщика копий базы в проекте нет и не требуется:
политика — внешние ежедневные снимки VPS. Разрушающие изменения схемы
требуют отдельного решения и окна обслуживания.
