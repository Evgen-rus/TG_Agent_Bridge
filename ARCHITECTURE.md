# AgentBridge architecture

This document is a compact orientation map for future Codex work. The current
code and tests are authoritative when they differ from this summary.

## Flow

### Sales Buddy (Сали)

Опциональный Sales-only контроль договорённостей включается через
`SALES_BUDDY_COMMITMENTS_ENABLED=true` (default); `false` отключает функцию. Rick reminder API, его
хранилище, настройки и сервис не используются. `sales/commitments.py` хранит
реестр, source event/quote, состояния active/paused/done/cancelled, quiet,
next_check UTC, revisions, журнал решений, обработанные события и связи с
outbox в четырёх additive таблицах SQLite Сали. Первый запуск фиксирует
max event ID; старые события служат только контекстом. Свой учёт обработки
независим от events.processed; отрицательная авторизация и pending media
блокируют соответствующий пакет. Валидированный результат и отметки событий
сохраняются атомарно; устаревший snapshot отвергается без потери событий.

Выделение обещаний использует отдельный ephemeral TOOL_FREE/read-only/deny_all
turn через SalesProvider, без замены или resume треда сделки и без изменения
его версии. Длинные материалы используют прежний _documents/chunk cache и его
границы 64 частей/120000 символов извлечения/180000 итогового контекста;
новые исходные события не обрезаются, старая история помечается truncated.
Код проверяет схему, точную цитату из нового события, принадлежность
записи своей теме, разрешённый переход и основание даты. Visual/uncertain
evidence изображения не подтверждает обязательство. Старые относительные
сроки не привязываются к загрузке. Советы, предложения и неотправленные
черновики исключаются инструкцией извлечения; семантика остаётся ответственностью
модели. Сходные записи сопоставляются моделью по существующему ID; стабильный
action_key дополнительно подавляет дубли. Нераспознанное основание даты
отклоняет пакет, события сохраняются для повтора.

Минутная asyncio-задача обрабатывает новые пакеты и проверяет наступившие сроки
локально, без модельных вызовов на пустом тике. SDK turn использует общий service
lock и прежнюю cancellation discipline; обычные запросы имеют приоритет.
Обычный анализ получает компактный реестр как недоверенные данные. После
обычного ответа текущие события также актуализируют реестр; при рекомендации,
готовности данных и отсутствии client_draft/fact_review приложение может
добавить один вопрос в последнюю часть ответа, если он помещается без нового
сообщения. Этот вопрос не меняет публичную sales output schema или инструкции
сохранённого треда. В recommendations payload допускается внутренний
`_commitment_check`, добавленный только приложением после валидации модели.

Даты: следующий будний день после явно названного срока; для текущего ожидания
ответа без срока — через три будних дня. Окно инициативы пн–пт 10–18 UTC+7,
не более одного автономного сообщения в день на тему. До трёх вопросов
объединяются в сообщение; после двух доставленных проверок запись quiet.
Контекстная проверка тоже сдвигает повтор на три рабочих дня. Счётчик меняется
атомарно с подтверждением outbox, не при генерации. Перед queue/send сравниваются
snapshot событий/реестра, авторизация, готовность media, непрочитанные события
и pending fact review; stale standalone удаляется из очереди, у обычного ответа
восстанавливается текст без вопроса. Неполученный ответ не является доказательством
невыполнения. Отключение сохраняет данные, но блокирует вопросы; после включения
старые проверки распределяются по ближайшим трём будним дням. Telegram сохраняет
прежнюю at-least-once семантику доставки.

Правила помощи менеджеру: локальная правка запрошенной фразы сохраняет остальной
draft; замечания из истории своей сделки учитываются при следующем ответе.
Общие правила формулировок закреплены в MANAGER_STYLE для всех сделок без передачи
чужой истории: конкретная польза ответа, выяснение причины неподходящего варианта.
При нехватке материала Сали просит его вместо общего совета; совет связывает
действие с ожидаемым ответом клиента. После отправки сообщения дальнейшая развилка
даётся лишь при продолжающемся запросе помощи, без выдуманных сроков/напоминаний.
Это доверенные инструкции, не автоматическое обучение на замечаниях менеджера.
Их изменение обновляет hash версии: на следующем анализе тред штатно создаётся
заново из SQLite; сохранённые события, факты и рекомендации остаются.

Изображения Sales-only: photo (наибольшее разрешение) и image-document определяются
общими describe_message_media/is_visual_media. Ingest атомарно сохраняет image_jobs
с inbox; image_pending исключён из контекста и блокирует свой batch до чтения.
ImageWorker использует общие download_telegram_file/media_destination, проверяет
20 МБ по metadata/remote/файлу и magic JPEG/PNG/WEBP/GIF, атомарно переименовывает
.part. Путь включает тему и event ID и проверяется внутри Sales media root.
SalesProvider получает только локальный путь и event/message ID с подписью,
без Telegram file IDs; существующий SDK runner передаёт TextInput + LocalImageInput
в ephemeral read-only/deny_all TOOL_FREE turn. Новый API/ключ не нужны.
Сервисный lock и cancellation discipline исключают конкурирующие SDK turn.

Структурированное evidence (readable_text/visual_observation/uncertain) сохраняется
атомарно с image_transcript в events. Все извлечения и текст/вопрос проходят общий
анализ сделки и прежний facts/outbox/checkpoint. Источники восстанавливаются из
image_jobs и для контекста, и для повторной проверки fact_reviews/DISC в commit.
Цитаты вне evidence/подписи отвергаются; visual/uncertain допускают только
assumption/unknown. Прочитанный скриншот — источник со слов переписки, не независимая
проверка. Исправление защищённого факта требует слов менеджера из подписи, а не
слова «теперь» внутри скриншота. DISC использует только readable_text, без подписи.
Нечитаемый текст не угадывается, GIF ограничен видимым кадром.

Download retry до пяти попыток с durable backoff и одним notice на стадию;
terminal failure явно превращает событие в unsupported_attachment, сохраняя
вопрос/подпись. Остальной пакет получает явное отсутствие изображения, без фиктивного
успеха чтения. Provider outage сохраняет pending image и общую штатную паузу/probe.
Checkpoint загрузки/чтения переживает рестарт; окно model turn/DB commit допускает
повторное чтение. После завершения обработки/отказа бинарник удаляется; evidence,
Telegram metadata и исходный source ID остаются. Rick flow не меняется.

Накопление Sales-only: events хранит received_at, deals — batch_cutoff_id.
Runtime pending_topics(batched=True) атомарно фиксирует границу по 15 секундам
тишины или 60 секундам накопления. process(batch=True) под lock заново проверяет
границу и unresolved auth/audio/doc; context(batch=True) читает все текущие
события до границы, без старого лимита 10, и не включает будущую историю.
Новые сообщения принадлежат следующему окну; SQLite сохраняет окна после
сбоев. 60 секунд ограничивают сбор ввода, не STT, модель или доставку.
Обычный доступ context/pending_topics остаётся прежним для диагностики.
Rick batching не менялся. Knowledge loader объединяет core.md и полную
manager_tactics.md; hash инструкций/знаний обновляет тред штатно. Внутренний
выбор тактики не расширяет output schema. Аудио без вопроса извлекает память
и выдаёт подтверждение, без next_action/goal или готового клиентского текста.

Обычный Sales-анализ, document parts и checkpoints используют validate_result
с check_semantics=False. Проверяются формат/размеры, event_id текущей сделки и
принадлежность заменяемого facts ID её карточке. Дословность quote, смысловые
regex неопределённости/исправления, kind=unknown, наличие/доступность basis и
запрет downgrade не блокируют ответ; классификация остаётся за моделью.
Замены фактов своей карточки применяются без нового Telegram-подтверждения.
Strict mode сохранён для ранее созданных fact_reviews, чтобы завершить их
прежний контракт. Проверки DISC, редактора и отдельного реестра договорённостей
не меняются. Две additive SQLite таблицы review_episodes/fact_reviews
сохраняют весь валидированный результат, исходные events, решения/участника/время.
В каждой теме не более одного ожидающего episode. До всех решений не меняются
ни facts, ни profiles, не доставляется ответ, который мог опираться на спорную
запись. Ingest продолжается; Codex для этой темы не вызывается повторно.

Карточки «Сейчас / Предложение модели / Основание» идут через прежний durable
outbox в исходную тему; кнопки «Подтвердить / Отклонить» проверяют текущую
membership, chat/topic и точный доставленный message ID. Все текущие участники
группы имеют это право по решению владельца; боты/anonymous/outsiders не допускаются.
Последнее подтверждение атомарно применяет весь результат и выпускает один ответ.
Любое отклонение сохраняет прежнюю память целиком и выпускает объяснение вместо
спорного текста; для нового анализа менеджер уточняет/повторяет вопрос. Stale
факт не замещается повторно. Повторные clicks/complete не создают фактов/карточек.
Подтверждение меняет только данные сделки; команд/кода/owner working memory нет.

ValueError валидации или некорректный результат codex_turn_failed останавливает
конкретный episode с processed=3 и одним безопасным сообщением, без глобального
codex_failure и без бесконечных retry. Исходные материалы остаются в SQLite;
processed=3 включается в history своей сделки вместе с processed=1, но не
запускает самостоятельный автоматический повтор. Неготовые media исключены.
Авторизация/лимит/сеть/транспорт сохраняют прежний backoff/probe. Непрошедшая
редактура использует исходный draft; лишь реальный provider outage ставит общую
паузу. Логи содержат фиксированный reason, никогда данные спорного факта.

v0.4: стиль обычных ответов задают доверенные правила Sepia/client-chat в
`sales/editing.py`, без дополнительных модельных проходов. Основной turn
помечает целиком готовый клиентский текст `client_draft=true`; только для него
есть отдельный ephemeral редакторский turn, effort=low, тот же TOOL_FREE,
read-only/deny_all и очищенное окружение. Файлы skills runtime-модель не читает:
правила заранее адаптированы из canonical professional/style/voice passes.
Редактор сначала возвращает дефекты, затем минимальные замены. Код допускает
только проверенные пары фраз, единственное вхождение на границах слов вне цитат,
неизменные numeric/link anchors, goal/next_action и preservation flags.
Любая непрошедшая замена отвергает весь проход. Произвольный пересказ не разрешён;
основная естественная формулировка создаётся до редактора. Ошибка редактора
сохраняет исходный готовый текст; недоступность провайдера ставит backoff будущим
анализам, не отменяя уже завершённый результат. Финальный checkpoint включает
редактуру, поэтому повтор после него не расходует новый редакторский turn.

DISC адаптирован из Neuro_rop_practice client_communication_profile_rules и
validation/manager_*: `sales/communication.py` проверяет status, стили,
low/medium/high confidence, guidance и точные evidence. Основной turn сначала
определяет роли по смыслу диалога, как в Neuro_rop_practice; подписи необязательны.
evidence.attribution хранит role/material/client_count/confidence/reason,
точный context_quote и identity_quote. Код проверяет цитаты в оригинальном
разговоре и границу speech_offset, источник, уверенность, явные manager labels
и идентификацию; семантическое определение роли остаётся гипотезой модели.
manager_summary/unknown/low не доказательства клиентского DISC. Имя Сергей и
автор Telegram не задают роль. Named contact требует исходной фразы идентификации;
client@event_id означает единственного уверенно различимого безымянного клиента
только этого разговора. Анонимы разных источников не сливаются; множественное/
неизвестное участие не получает общего портрета. Применение анонимного профиля
ограничено разговором, который менеджер продолжает. Старые v0.4 evidence без
attribution читаются прежней строгой проверкой подписей. SQLite schema прежняя:
дополнительные поля сохраняются в существующем JSON с историей ревизий.
У каждого подтверждённого контакта свой профиль. Реплики допускаются в тексте, документе,
feedback и распознанной речи; подпись менеджера аудио исключается. supported
требует двух разных содержательных цитат и достаточной уверенности. Повтор
одной цитаты не считается новым наблюдением. Недопустимые основания нейтрализуются;
при таком изменении финальный turn один раз исправляет prose по проверенным
профилям; повторный непроверенный profile update приводит к безопасному отказу.
Это исключительная коррекция ответа, не постоянный дополнительный DISC-анализ.
Проверка источника механическая; смысл наблюдения и соответствие DISC остаются
ответственностью модели, а явная подпись не доказывает реальную личность говорящего.

SQLite `communication_profiles` и immutable `communication_profile_revisions`
добавлены без изменения старых таблиц/данных. Profile updates атомарны с facts,
рекомендацией, outbox и processed. Исправление менеджера, в том числе голосовое, имеет точную цитату
текущего события и блокирует прежние источники; история ревизий сохраняется.
Без новых текущих доказательств/исправления прежний профиль не стирается.
Контекст содержит последние 8 профилей по теме; доказательств до 5, в prompt
до 50 инвалидированных ID, полный список остаётся в SQLite и перепроверяется
внутри complete. Длинные материалы передают наблюдения каждой части в итоговый
тред; единственный финальный профиль сохраняется после анализа всех частей.
Изменение инструкций v0.4 обновляет thread version штатным механизмом: следующий
анализ создаёт тред из SQLite, прежние карточки и материалы сохраняются.

`sales/activity.py` повторяет Rick asynccontextmanager/create_task/finally-cancel
подход без изменения Rick bot. sendChatAction(typing) сразу и каждые 4 секунды
с точным chat/topic. Общий refcount registry на тему объединяет одновременно
работающие audio/model scopes. Scope анализа открывается только после lock,
available и наличия событий, включает все части, Codex и редактуру до checkpoint.
Scope аудио включает download/STT, не backoff. Ошибка Telegram не мешает работе.
Отмена закрывает typing сразу, хотя lock удерживается до окончания SDK worker.
Shutdown сначала закрывает все typing loops, затем обычные задачи. Outbox,
polling и восстановление сохранены; typing в SQLite не хранится и в ожидании
доставки или очередного retry не показывается. Telegram может сохранять последнее
показанное действие несколько секунд: отдельного API «выключить typing» нет.

`python -m agentbridge.sales.main` запускает отдельный Telegram-бот, не меняя
Rick application/provider/settings/Telegram handlers. Конфигурация использует
`TELEGRAM_SALES_BADDY_BOT_TOKEN`, `OWNER_SALES_BADDY_CHAT_ID` и отдельные
`OWNER_SALES_BADDY_CODEX_*`. Только указанная forum-супергруппа и её текущие
участники допускаются; боты, анонимные отправители, другие чаты игнорируются.
Каждая пара `(chat_id, message_thread_id)` — отдельная сделка. Ответы Сали идут
не в Rick OWNER_CHAT_ID, а непосредственно в исходную тему.

Ingest сохраняет текст, автора, дату, message/update ID, источник и reply_to
в отдельную `runtime/sales_buddy/sales.sqlite3` до фонового анализа. SQLite
хранит историю, карточку с source quotes, рекомендации и очередь частей ответа.
Повторы updates/messages подавляются уникальными ключами.
При временной ошибке проверки участника сообщение хранится в held-состоянии
(`processed=-1/-2`) и не попадает модели до успешной повторной проверки.
Отказ авторизации (`processed=2`) исключает его из истории для модели.
Карточка дополняется
патчами: старый подтверждённый факт замещается только при явном исправлении
или изменении в новом событии, исходный факт и сообщение сохраняются. Источники
и принадлежность фактов теме проверяются кодом; семантическая точность пересказа
остаётся ответственностью модели. Ответ менеджера на рекомендацию получает
источник `manager_feedback`, а reply_to ссылается на доставленное сообщение.

SalesProvider отделяет Telegram от модели. SalesCodexProvider переиспользует
структурированный SDK runner CodexProvider без Rick prompts, памяти владельца,
knowledge pack LeadGenBureau или клиентских тредов. У каждой пары chat/topic свой
постоянный thread_start/thread_resume; ID/version/число успешных анализов — в
deals. ID сохраняется после start до первого turn. Новый тред создаётся только
при явно недоступном rollout, изменении инструкций или после 100 успешных анализов
для уплотнения из карточки/источников. Timeout/лимит/сеть сохраняют прежний ID.
Sales-only config_overrides задаёт model_auto_compact_token_limit=150000:
Codex автоматически сжимает историю в том же треде при достижении порога,
независимо от счётчика успешных анализов. model_context_window не переопределяется.
Новые треды Сали и редактора передают свои правила только через
developer_instructions, без base_instructions override. Существующие треды
продолжаются без смены версии ради этой правки; их сохранённая база не заменяется.
Основные turn
read-only, deny_all, tools/apps/MCP/plugins/hooks/memories отключены. Эквивалент
восстановления диалога — компактная карточка, последние/связанные первоисточники
и пять рекомендаций из SQLite. Codex home отдельный, credentials других
интеграций в окружении дочернего процесса очищаются. По явному решению владельца
процесс запускается под тем же Unix-пользователем `rick`: это разделение данных
и инструментов, не OS-изоляция от файлов, доступных этому пользователю.

Анализ и доставка — независимые фоновые задачи. Ошибка анализа не меняет
карточку и оставляет events pending; повтор через 5 минут. Commit успешного
анализа атомарно сохраняет карточку, рекомендацию, outbox и processed.
Доставка возобновляется после рестарта без нового анализа. У Telegram остаётся
send/DB-ack crash gap: сообщение может повториться после аварии в этом окне.
Неадресованные ответы участникам, упоминания других пользователей и короткие
подтверждения сохраняются как контекст, но не вызывают модель сами по себе.
Ссылка reply_to на начало текущей темы не считается ответом другому участнику.
Обращения Сали/Салли/Sali/Salli/Saly/Sally (без учёта регистра) и @username
бота явно вызывают ответ; продолжение через reply на сообщение бота сохраняется.
UTF-8 txt/md до 2 МБ сначала сохраняют метаданные в durable inbox, затем фоновой
загрузкой полный текст. Авторизация/временная ошибка скачивания оставляет запись
ожидающей; file IDs не попадают модели. PDF/DOCX явно непрочитаны.
Voice/audio/audio-document сохраняются атомарно с inbox в audio_jobs: metadata,
caption, путь к временному файлу, transcript, state, attempts, retry_at/error_kind.
audio_pending исключён из facts/history/model processing, включая held ingress.
Отдельный AudioWorker переиспользует download_telegram_file/media_destination,
MAX_DOWNLOAD_BYTES=20МБ и общий transcribe_audio_file. Проверяет Telegram size до
скачивания, реальный размер после и magic контейнера; для API нормализует suffix.
Поддержка контейнеров сверена с OpenAI Audio API (ссылка в README). Файл .part
атомарно переименовывается и путь сохраняется до STT. STT success атомарно сохраняет
транскрипт и заменяет audio_pending на audio_transcript, разделяя подпись и речь.
Для новых audio_jobs решение отвечать принимается после STT по подписи и
распознанной речи; routing metadata сохраняют username бота и адресацию reply
через рестарт. Старые jobs без этих metadata сохраняют прежнее решение.
Прежние Codex threads/версии инструкций сохраняются: аудио поступает туда как
обычный текст, длинный — через существующий document pipeline. После успешного
анализа worker удаляет аудио, сохраняя metadata/file ID/transcript в SQLite.
До пяти логических попыток с backoff; permanent API auth/quota/request, oversized,
unknown container или empty transcript -> failed/processed=3 и безопасный outbox
notice, без Codex/фактов/фиктивного processed=1. После окончательного отказа нужна
повторная отправка. Окно STT success/DB checkpoint допускает повторный расход API.
Отдельный env Сали содержит OPENAI_API_KEY/TRANSCRIPTION_MODEL; ключ repr=False,
не попадает в Codex child env. Rick settings/модель не меняются; общий file helper
передаёт basename вместо постоянного voice.ogg (bytes API/language contract прежний).
Текущие события передаются полностью. Материалы длиннее 16000 символов
последовательно разбираются ephemeral внутренними turn; document_chunks хранит
извлечения/цитаты по event ID/части/версии и восстановление не перечитывает готовые
части. Все извлечения поступают основному persistent треду, результат один.
Пределы: 64 части на эпизод, 120000 символов извлечения, 180000 итогового контекста.
Превышение сохраняет полный исходник с processed=3 (явно незавершённый анализ),
сообщает пользователю, не создаёт рекомендации и не повторяет без конца.
turn_results хранит успешный валидированный ответ до атомарного complete, поэтому
авария после checkpoint не повторяет model usage. Окно turn/checkpoint остаётся:
повтор возможен, но request key/SQLite и unique commit исключают второй итог.
Отмена asyncio не освобождает lock до завершения SDK worker; один тред не получает
конкурирующих turn. В prompt берётся до 16 прежних
источников, до четырёх последних элементов каждого поля, 12 старых фактов
по совпадению слов нового сообщения и исходных client/subject. Признак
omitted_facts сообщает о неполном снимке; весь ledger остаётся в DB без лимита
числа фактов. Полные сообщения остаются в DB.

operations/restarts и существующий outbox Сали сохраняют run ID/clean shutdown,
состояние недоступности Codex, уведомления и подтверждения. Служебная доставка
идёт в General (topic=0), опционально SALES_BUDDY_SYSTEM_TOPIC_ID. Startup probe
асинхронный, ephemeral и реальный; очередной запуск при активной паузе сообщает
сохранённую недоступность, не объявляет готовность. Проверки восстановления
ограничены экспоненциальной паузой (5 минут, при лимите час, максимум 6 часов).
Новый успешный probe снимает паузу и открывает накопленные события. Классификация
лимита/авторизации/транспорта переиспользует Rick helpers; сеть выделяется отдельно,
сырые transport errors Сали фильтруются без изменения диагностики Рика.
PollingWatchdog/HeartbeatHTTPXRequest переиспользованы. Admin-only restart требует
запроса и одноразового callback того же администратора, точного сообщения/темы,
актуальных прав и TTL 10 минут. Durable claimed/launched записывается до systemctl.
Команда и отдельное sudoers правило допускают только sales-buddy.service; Rick
restart launcher не используется. При сохранении старого процесса через 15 секунд
Сали сообщает, что остановка не подтверждена, и продолжает работать.


```text
Telegram long polling (drop_pending_updates=False)
  -> hard-deadline getUpdates transport + monotonic polling heartbeat
  -> watchdog restarts a stalled updater, or fails the process if recovery fails
  -> persist each relevant update in SQLite (durable inbox/history)
  -> transcribe voice notes in the background (TRANSCRIPTION_MODEL via
     AgentBridge.transcribe, transcript stored by update_id in SQLite)
  -> download current-episode photos/documents via saved file_id into runtime/media
  -> live debounce per chat, or catch-up after restart
  -> application: episode from stored messages, context pack, Rick model turn
  -> AgentProvider (images as LocalImageInput, other files as MentionInput)
  -> CodexProvider: start or resume the chat's Codex thread
  -> optional ephemeral Sepia refactor turn with draft + compact facts only
  -> lightweight fact/commitment flags + exact number/link guard
  -> delete temporary photo/voice copies; retain documents and file_id in SQLite
  -> update chat_state
  -> telegram.formatter
  -> OWNER_CHAT_ID only
    action: reply | ask_owner | observe | no_action

Owner reminder commands are stored in SQLite with an absolute UTC due time.
The existing owner delivery retry loop sends due reminders only to
`OWNER_CHAT_ID` and marks them sent after successful Telegram delivery. A
restart therefore delivers overdue pending reminders without requiring a
separate scheduler.

An explicit Codex `usage_limit_exceeded` is a durable, owner-visible condition,
not an in-memory flag. `operational_state` holds the notice key, the display
reset hint, the reset moment in UTC derived from the assumed Codex session
zone, and the last time the limit was seen. On startup `main` reads that state
and injects it into both `CodexProvider` instances, so a restart neither loses
the condition nor re-sends the notice. Recovery is the first successful turn
after the limit, which clears all limit state and queues one recovery notice.
While the limit is active the owner query path answers from SQLite instead of
calling the model, but only until the reset moment; after it, or after a
bounded cooldown when Codex named no reset time, the next real owner request is
allowed through as the recovery probe. There is no background polling and no
extra model turn.

Because two `CodexProvider` instances exist (client chats and owner contour) and
their in-memory state diverges, SQLite alone decides whether a limit is new. A
provider only reports facts: `on_usage_limit(reset_hint)` on every explicit
usage-limit error and `on_usage_recovered(turn_started_at)` on every successful
turn. Storage resolves both into `claim_codex_usage_limit` and
`claim_codex_usage_recovered`, each a single `BEGIN IMMEDIATE` transaction that
checks and updates the notice key, so a provider that still remembers an old
limit can neither suppress the notice for a new one nor duplicate a recovery
notice. A provider never touches storage directly.

Recovery is causal, not just chronological. A turn that started before the
current limit was recorded proves nothing about it — it may have been accepted
on the last of the previous allowance and returned after a sibling turn was
refused. `claim_codex_usage_recovered` therefore compares the turn's start
moment with the limit's `seen_at` and refuses to clear the limit when the turn
predates it, logging `codex_usage_limit_recovery_ignored
reason=turn_started_before_limit`. A turn whose start moment is unknown is
accepted, because refusing would strand the limit in the database forever.

The transition into a new limit is atomic: clearing the previous recovery
marker, queuing the owner notice, setting the active marker, and writing the
reset/seen state all commit in that one transaction. Callers no longer touch
lifecycle keys themselves.

OWNER_CHAT_ID
  -> reply to a bot recommendation: correction / learning / memory
     A substantive owner correction may also create a pending memory draft;
     it is delivered separately with «Для всех», «Только этот чат» and
     «Не сохранять» buttons, and scope is applied only after the button press.
  -> reply to a proactive question: fill the knowledge gap
  -> mention/tag of the bot: assistant query
  -> «Общий контекст: …» in the owner chat: confirmable global memory,
     without a client chat or a reply to a recommendation
  -> reply to a “which chat?” clarification or to the last assistant answer:
     continue that query
  -> portfolio query: resolve single/multiple/all scope; ambiguous scope gets
     durable owner-only selection buttons, then isolated per-chat summaries and
     owner aggregation. Periods are converted from OWNER_TIMEZONE to [from,to)
     UTC before SQLite history filtering.
  -> substantive owner-assistant answers are delivered as full text to
     OWNER_CHAT_ID first; only then may a speech provider synthesize an MP3
     that is sent as a separate message. The selector checks current model
     pricing against a zero-dollar default ceiling, so unknown-price and paid
     routes are skipped. Audio remains in the existing durable owner-query
     delivery queue until Telegram accepts it.
  -> reply to a new-group card: client brief, then confirm wiki draft
  -> /rules and /undo from the owner-chat command menu only
  -> ordinary human conversation is ignored

After a substantive owner query or a completed general/developer/LeadRecord
task, the application schedules a best-effort memory update without awaiting it
before returning the owner result. The dedicated Memory Agent receives only the
current `owner_context/working_context.md` and that interaction's request,
outcome, type and compact work summary. It never receives SQLite owner/client
history, client context packs or raw client history. Typing, callbacks, startup
notices, duplicate updates, acknowledgements and TTS do not trigger updates.
Memory failures do not affect owner delivery.

Ambiguous owner queries include an explicit «Общая задача» choice. This path has
one persistent owner-only Codex thread under reserved key `0` and receives no
client wiki, history, state, or scoped memory. It first stores a durable task
plan and waits for «Да, чувак, погнали!», clarification, or cancellation.

Selection callbacks are confirmed before the long work, not after it. A final
action (`done`, `all`, `general`, `choose`) immediately clears the keyboard,
edits the card to an acknowledgement, and shows typing while Codex runs, so a
press is never mistaken for a hang. List toggling (`item_add`, `item_remove`,
`reset`, `multi`, `single`) keeps the keyboard in place because it answers
fast. Re-entrant presses are safe by construction: `claim_owner_query_selection`
moves the selection out of `selecting` atomically, so a repeat runs no second
analysis, and a failed turn reports an explicit message instead of leaving
disappeared buttons.
Natural-language reminders are planned by Codex but, after confirmation, use
the existing SQLite reminder API and delivery loop instead of an ad-hoc command.
A confidently resolved single client is planned the same way before any chat
analysis: `kind=reminder` stores that chat as a label, and any other kind keeps
the ordinary owner query. Delivery stays in `OWNER_CHAT_ID` and mentions the
Telegram user who created that reminder. Old reminders without an author stay
untagged.
Replies to any delivered result of a completed general task start a new
confirmable general task in the same persistent owner thread.
An explicit self-restart request is also planned there; deterministic code
performs it only after confirmation. Under Linux systemd it requests only
`/usr/bin/sudo -n /usr/bin/systemctl --no-block restart rick.service`.
SQLite keeps the restart marker. Every new process queues an owner-only startup
notice after Telegram starts, then makes one fresh, ephemeral read-only Codex
turn through the owner provider and queues the success or failure result. Both
notices use the existing durable delivery queue and a per-run key, including
after a manual systemd restart. The new process acknowledges a self-restart
marker after queueing the startup notice; it sends no extra restart message.

The owner can enter developer mode with `/dev` or «Рик, режим разработчика»
and leave with `/dev off` or «Рик, обычный режим». The mode is persisted per
owner-chat user in SQLite. While active, that user's owner-chat messages plan
code tasks in a reserved owner thread under key `-1`, separate from general
tasks (`0`) and all client threads. Each code task requires the existing owner
confirmation card. The confirmed Codex turn uses `workspace_write` in the
current project checkout with denied sandbox escalations; ordinary turns stay
read-only. Trusted code then runs the fixed test/compile/dependency/diff checks
and reports their status. Applying code still requires a separately requested
restart. This is conversation isolation, not OS-level isolation of project files.

At startup SQLite records `operational_state.run_status=running`; a clean
shutdown writes `stopped`. A previous `running` value means an unclean stop,
so the new process queues one owner notice in `owner_query_deliveries`. The
exact cause may remain unknown until systemd logs are inspected. The same
delivery queue stores the deterministic 07:30 Europe/Moscow daily report.
`daily_reports.report_date` prevents duplicate creation and advances missed
reports after downtime. The report is built entirely from SQLite and never
calls the model; it counts every active durable memory kind (fact, decision,
commitment, preference, open question, rule, assumption, experience) as one
«запись памяти», and keeps rules and experience as separate counters. Draft,
rejected and superseded records are not counted. Operational error counts are
stored without messages.
```

Each monitored chat has isolated persistent Codex threads: the client thread
handles client-message recommendations, the owner-query thread continues the
team's internal questions, and the optional Sepia thread edits final replies.
All receive fresh input on every turn; none is a durable source of truth. The
client thread is resumed without reloading its turns; if its saved rollout is
no longer available, Bridge starts one replacement thread and persists that ID
after the successful episode, so later messages resume the replacement.
The two paths use separately configured `CodexProvider` instances: `CODEX_MODEL` /
`CODEX_REASONING_EFFORT` for client work and `OWNER_CODEX_MODEL` /
`OWNER_CODEX_REASONING_EFFORT` for internal Owner queries, feedback analysis,
and onboarding drafts. Regenerating a client recommendation after confirmed
feedback still uses the client provider. When `SEPIA_ENABLED` is true, only a
final `reply` draft is sent to the project-local `sepia-refactor` skill together
with the `client-chat` voice profile. Sepia gets no Telegram history or media.
If its preservation flags fail or an exact number/link changes, the original
Rick draft is kept. Owner queries, observations, and questions are not refactored.

If the bot becomes admin in an unknown group, or a message arrives from a
group where it is already admin but there is no `chats/*/config.yaml`, the
owner gets a card. Client messages are stored as `held` and are not sent to
Codex until the owner confirms the wiki draft. Telegram does not list the
bot's chats, so a silent already-admin group is discovered on the first
visible message or membership update.

Telegram updates accumulated while the process was offline are ingested first,
then processed as catch-up episodes. `post_init` does not wait for catch-up:
polling starts, backlog is stored, live analysis stays off until a short idle
window after the last ingested update, then catch-up runs. Only the final
episode may notify the owner. `MESSAGE_BATCH_SECONDS` (20 by default) is the
live debounce. `CATCHUP_IDLE_SECONDS` is the startup quiet window.
`CATCHUP_EPISODE_SIZE` splits a large backlog. Different chats never share an
episode.

Polling liveness is independent of message traffic: every completed or failed
`getUpdates` attempt advances a monotonic heartbeat, including empty responses.
A hard request deadline prevents a single HTTP call from waiting forever. If
the heartbeat becomes stale, the in-process watchdog stops and restarts the
Updater with `drop_pending_updates=False`. If that bounded recovery fails, it
requests application shutdown and `main` exits non-zero so an external
supervisor can restart the whole process. The SQLite last-message time is not a
health signal because all monitored chats may legitimately be quiet.

A large backlog is split into ordered episodes (`CATCHUP_EPISODE_SIZE`). Only
the final episode may notify the owner, so stale early questions do not create
a series of outdated recommendations.

## Components

The daily report transaction also inserts a `self_audit_runs` job. The existing
Telegram delivery retry loop starts `SelfAudit` as an independent asyncio task
only after that report's delivery ID is linked. Collection and model work run
outside the event loop. Windows cover 24 hours before report queueing plus the
preceding seven days; delayed reports coalesce their audits into the newest job.
At startup, interrupted `running` jobs become `pending`. Completed/failed jobs
are never replayed automatically. An explicit durable Codex limit skips a turn.

`self_audit.py` reads only allowlisted technical log fields and SQLite counters,
with source row/line references and partial-coverage notes. It never reads Codex
session files, client message bodies, prompts or working context. CodexProvider
records only completed tool kind/status/duration and fixed failure categories.
Process-local HMACs of textual turn inputs detect exact repeated requests without
retaining prompts or a recoverable key. Cross-restart and media-input comparisons
are unavailable; repeated requests alone do not prove wasted model usage.
Successful same-thread turns after failures and same-kind tool recoveries are
indicators, not proof of a causal fix or useful/pointless work.

The audit uses a fresh ephemeral read-only Codex thread in `runtime/self_audit`,
without owner-context instructions, inherited tools, hooks or approvals. Output
is limited to three proposals with validated evidence keys and bounded text.
`self_audit_proposals` stores cards, canonical event identity, evidence and owner
decisions. Creation/completion is atomic; delivery reuses durable message parts.
Private-chat callbacks require the owner's user ID; group callbacks require
`SELF_AUDIT_OWNER_USER_ID`, and both bind to the delivered message ID. Acceptance
only records consent: implementing code still requires a new confirmed `/dev`
task. No audit callback enters learning, Memory Agent or general-task execution.
Rejected event identities return only with doubled frequency and at least three
additional cases, or a new fixed failure category. Accepted, pending and deferred
identities stay suppressed. Telegram's existing send/DB-ack crash gap remains.

- `agentbridge/main.py`: composition root and polling lifecycle.
- `agentbridge/settings.py`: `.env` configuration.
- `agentbridge/telegram/`: Telegram adapter, persist-first ingest, per-chat
  batching, owner mention/reply interface, and owner-message formatting.
- `agentbridge/application.py`: use-case orchestration, context pack, catch-up,
  chat_state updates, and owner assistant queries.
- `agentbridge/owner_query.py`: owner-query scope/summary value objects and
  deterministic local-time to UTC period parsing.
- `agentbridge/owner_memory.py`: validation, atomic persistence and
  path-scoped local commit for the OWNER working-context Markdown file.
- `agentbridge/agents/base.py`: provider boundary and action types.
- `agentbridge/agents/codex.py`: official `openai-codex` implementation and
  structured Codex output.
- `agentbridge/chats/loader.py`: loads `chats/*/config.yaml` plus sibling
  `wiki.md` into a registry keyed by Telegram chat ID.
- `agentbridge/knowledge.py`: compact shared knowledge pack loader (`core.md`
  plus an on-demand file index).
- `agentbridge/media.py`: temporary photos/voice copies (TTL 1 hour), retained
  document copies, and labels for history.
- `agentbridge/transcribe.py`: OpenAI speech-to-text call used for client voice
  notes; the configured `TRANSCRIPTION_MODEL` is the only transcription model.
  `gpt-transcribe` receives `languages` via SDK `extra_body`; other models
  retain the singular `language` hint.
- `agentbridge/speech.py`: reusable speech-provider interface, ordered provider
  selector with a per-response cost ceiling, and the OpenRouter TTS adapter with
  a short-lived in-memory pricing catalog cache.
- `agentbridge/storage/sqlite.py`: durable Telegram history, threads, chat
  state, processed updates, pending/delivered recommendation links, learning
  drafts, versioned rules, memory, owner questions, reminders, experience, and
  Telegram `file_id` metadata for client attachments.
- `agentbridge/logging.py`: secret redaction and seven-day daily diagnostic
  file rotation.

## Memory

Durable context layers, in order of authority:

1. SQLite Telegram history for monitored chats. This is the source of truth for
   what was said. Codex thread is continuity only.
2. `chats/<name>/wiki.md` contains stable, manually maintained knowledge and is
   included in every model turn for that chat.
3. `knowledge/<pack>/core.md` is compact company knowledge. A chat receives it
   when `knowledge_pack` is set; the default for loaded chats is
   `leadgenbureau`. Use `knowledge_pack: none` to opt out. Only the core and a
   file index go into the prompt; extra documents stay on disk for on-demand
   reads. Chat wiki and confirmed chat memory override the shared pack.
4. `chat_state` is a compact working snapshot (facts, decisions, commitments,
   open questions, next step). It is updated after a successful episode.
5. Confirmed memory entries have `fact` / `decision` / `commitment` /
   `preference` / `open_question` / `rule` / `assumption` / `experience` kinds
   and an explicit `chat`, `project`, or `global` scope.
6. Confirmed learning rules and recent confirmed experience are added to the
   context pack when relevant.

### Experimental OWNER working context

`owner_context/working_context.md` is Git-tracked current state, not a
conversation history; Git provides its change history. The primary owner
provider adds it to owner prompts as explicitly untrusted/model-maintained data.
It cannot override developer/system instructions, permissions, sandbox or
security policy. The client provider never receives it.

The Memory Agent uses reserved `owner_query_threads` key `-2`, separate from
general task key `0`, developer key `-1`, and client thread tables. It uses the
OWNER provider's configured model/reasoning in `read_only`/`deny_all`, with cwd
limited to `owner_context/`. The database stores its thread ID and prompt
version for continuity; the Markdown remains the recoverable source of truth.
No database schema change is needed.

Trusted Python validates the structured response and fixed Markdown headings,
checks credentials and size, writes by atomic replacement, then runs a
path-scoped `git commit --only` for this file alone. The initial target is 8000
characters, soft limit 10000, hard limit 12000, plus a 16 KiB UTF-8 guard. A
single compaction turn may run on the same memory thread. An oversized or
invalid result leaves the previous state. If Git fails, the new file may remain
locally and the error is logged; owner flow stays successful. There is no
push, automatic retry loop, or project commit from this runtime task. Set
`OWNER_WORKING_MEMORY_ENABLED=false` to disable both owner context injection
and memory updates.

Before a model turn the application builds a compact context pack: wiki, shared
core if attached, current `chat_state`, recent history, the current episode,
confirmed memory, rules, and recent experience. Do not send the whole archive
or the whole knowledge pack.

SQLite also stores recent internal messages when an active learning rule for
that same chat identifies the sender as internal; global learning rules are
not used for participant classification. There is no global sender
registry: a name learned in one chat does not classify the same name in
another chat. Those messages never create an owner recommendation by
themselves, but they are part of mixed episodes and later
local context.

Recommendations are persisted before owner delivery. A missing owner message ID
means the recommendation is pending; the Telegram adapter retries pending rows
periodically and on startup. All owner text is split losslessly into numbered
parts when needed to fit Telegram's 4096-character limit (with a conservative
UTF-16 budget including part headers). Confirmation buttons appear only on the
last part. SQLite `owner_delivery_parts` freezes the split and records each
accepted part. Recommendation and owner-query retries, including after restart,
skip acknowledged parts and mark the whole delivery complete only after its
final part. Replies to any part of a completed message are resolved to the final
message ID, preserving recommendation, ASK_OWNER, and query-continuation links.
Other multi-part owner notices use the same reply mapping; only existing durable
delivery queues are retried automatically. This is at-least-once delivery: a crash
between Telegram acceptance and SQLite acknowledgement can duplicate that part.
Telegram BadRequest rejections are logged separately from network/time-out errors,
without logging raw exception text that could contain credentials.

A newer rule with the same semantic conflict key supersedes the old version.
Global scope also requires explicit global wording in the owner's feedback.
Before a new candidate is offered, active memory, active rules, and the
relevant knowledge-pack markdown are checked with normalized exact or
containment matching; this check does not change prompt packing.

Owner voice replies are generated only for final assistant answers. Plans,
selection prompts, routine notices, and generated images stay text-only. The
full answer is delivered as text first, and only after Telegram accepts it is a
separate MP3 voice note synthesized and sent. A TTS or audio failure therefore
cannot lose the answer. Both stay owner-only. The text parts use the existing
acknowledged delivery parts, while `owner_query_deliveries.media_kind` keeps
pending audio paths for retry; an audio-only row carries no text, so retrying it
never re-sends the text and never re-runs a paid synthesis.

Synthesis receives the same sanitized text as Telegram, so Codex file-citation
markup and local paths cannot reach the provider. A model is selected only when
its price is known and not above `OWNER_VOICE_MAX_COST_USD`; the default is
zero, with no paid fallback. The OpenRouter pricing catalog is cached in memory
for a short TTL instead of being fetched per answer; a failed refresh never
extends the cache, so price is re-checked rather than served stale.

`ASK_OWNER` is used when a needed fact is missing. The question is sent only to
`OWNER_CHAT_ID` and linked back to the client chat. The owner's reply can update
the current situation and propose confirmed memory; it is never sent to the
client automatically.

## Invariants

Portfolio owner queries do not reuse client Codex threads. The client provider
creates one stateless structured summary per selected chat (at most four in
parallel) from that chat's wiki, state, scoped memory/rules/knowledge and at
most 200 messages. The owner provider receives only compact summaries or a
failure stub for aggregation. `owner_query_selections` persists an ambiguous
selection and atomically claims it for processing; `owner_query_prompts` keeps
selected IDs and time metadata so a follow-up reruns the same portfolio.

- Suggest-only: never reply automatically to a monitored/client chat.
- One Telegram chat maps to independent client, owner-query, and optional Sepia Codex threads,
  plus one wiki, history, and `chat_state`. Each thread is continuity only and
  is stored with `prompt_version`. If `AGENT_PROMPT_VERSION` in
  `agents/codex.py` changes, the next client episode or owner query starts its
  respective new thread instead of resuming the old one. Sepia follows the
  same version boundary. Critique uses a
  separate ephemeral Codex thread and never overwrites `codex_thread_id`.
- General owner tasks never execute before their durable Telegram confirmation;
  duplicate callbacks cannot run a task twice. Arbitrary failed tasks are not
  retried automatically, while reminder delivery keeps its existing retry path.
  The single exception is a read-only owner/general turn interrupted by a closed
  Codex transport: it is retried exactly once on a fresh transport. A closed
  transport does NOT guarantee the question never reached the model, so that one
  retry may repeat model usage; it is accepted because the turn is read-only and
  has no application side effects. A second failure keeps the
  `CodexTransportClosed` type and takes the ordinary failed path. Every other turn
  error — including usage limit — is never replayed; a failed `thread_resume`
  starts a new thread but a failed turn does not.
- Creating a new image is an owner-only general task. It makes no OpenAI Images
  request before confirmation; after confirmation it generates one JPEG, stores
  it under `MEDIA_DIR/owner_generated`, and persists the path in the owner
  delivery queue. Telegram sends it only to `OWNER_CHAT_ID`; a successful send
  records its message ID and deletes the temporary file, while failed delivery
  retains the file for the retry loop. Generation uses `OPENAI_API_KEY` and is
  billed separately by OpenAI. Editing existing images is not supported.
- Self-restart never gives Codex process-control arguments: it can classify the
  intent only. The Telegram adapter starts the fixed local helper after owner
  confirmation, and only the replacement PID can close the restart marker.
- Wiki is read-only at runtime, except creating a new `wiki.md` after confirmed
  onboarding.
- Owner working memory is isolated from client turns, treated as untrusted
  data, and committed only by trusted code with a one-file local Git commit.
- Bot-authored messages and commands do not invoke Codex.
- Senders identified by an active, chat-scoped internal-participant rule are
  captured as local context and do not create a recommendation on their own.
- Cross-chat context is limited to explicitly confirmed project/global memory
  and the attached shared knowledge pack; raw client-chat context remains
  isolated. Shared cases are internal and must not be retold to another client.
- A message is not marked processed until episode logic finishes. A model
failure leaves the inbox row pending.
- A live Python process is not considered proof of Telegram polling health;
  `getUpdates` heartbeat progress is monitored independently and stale polling
  is recovered without dropping Telegram backlog.
- Client photos, documents, and media albums are analysed in that chat's Codex
  thread. Document copies remain under `runtime/media`; SQLite keeps Telegram
  `file_id`, timestamp, sender role, forwarding origin, and download status.
  Owner queries list that chat's document metadata and attach only documents
  whose numbers are explicitly mentioned in the question. Missing copies of
  processed documents are re-downloaded from saved `file_id` when the owner
  queries that chat. The catalog's live file status takes precedence over old
  `chat_state` availability claims; matching stale claims are cleared when a
  copy is available. History and episodes keep the actual sender separate from
  `forwarded_from`. Telegram delivery strips Codex file-citation markup and
  local filesystem paths. Photo and voice copies remain temporary. The bot cannot
  scroll a group's history like a user account.
- Voice notes follow the same persist-first path: the row is stored before any
  model work, then transcribed in the background during the batch window (or at
  catch-up after restart). The transcript is stored by `update_id`; Codex sees
  `[голосовое] текст`, never the audio file. API failure or silence becomes a
  placeholder so a batch is never blocked; without `OPENAI_API_KEY` voice rows
  simply stay pending. Owner-chat voice notes are transcribed on the fly and
  then processed as ordinary owner text (feedback, answers, mention queries).
- Tokens and credentials never appear unredacted in logs or tracked files.
- Owner-visible failures carry a fixed reason label plus a what-to-do hint
  (`codex_sandbox_missing`, `codex_transport_closed`, `codex_usage_limit`,
  `codex_auth`, `codex_turn_failed`). The raw error text stays in the
  redacted log; only the label and the hint reach the owner chat. The
  application layer asks the provider through `AgentProvider.explain_failure`
  and never imports Codex.
- Owner-group conversation invokes the agent on reply-to-bot, an explicit
  mention/tag, a voice transcript beginning with `Рик` or `Агент`, or a global
  memory prefix (`Общий контекст:`). Ordinary owner-group talk is ignored.
  Telegram media downloads use bounded retries. If an owner voice download
  still fails, its error message temporarily retains the original reply target
  so an immediate voice retry continues the intended bot-message chain.
- `AgentProvider` is the only application-to-agent dependency; Telegram code
  must not depend on Codex SDK details.

## LeadRecord owner analytics

The owner planner can return leadrecord_analytics inputs. Trusted leadrecord.py resolves configured groups through restricted SSH lkctl commands, refreshes snapshots and polls existing backend jobs after the ordinary task confirmation. It preserves the inclusive envelope plus up to 64 ordered periods (within a maximum 366-day envelope); confirmation, resumed runs and results must match the same period list in the same order. A single legacy start/end range is normalized to one period. XLSX is fetched below an isolated `runtime/media/owner_generated/leadrecord-task-<id>/` directory, using the validated `download_filename` supplied by LeadRecord, and delivered as media_kind=document through the existing durable owner delivery queue. Period metrics are reported separately when the requested periods overlap; they are never summed. run_id is persisted in owner_general_tasks.payload_json and restored only if group scope and the exact ordered periods still match. No client chat sends or unrestricted model shell are added.

## Редактирование Telegram-сообщений

Рик и Сали получают `edited_message` при обычном polling и восстановлении
watchdog. Правка сохраняется отдельной неизменяемой версией в существующем
inbox с исходным `message_id` и `edit_update_id=update_id`; повтор одного update
не создаёт новую запись. Старые версии, факты и ссылки на источники сохраняются.
Модели передаётся явная метка замены прежнего текста; исправление учитывается
в обычном окне своей беседы/темы, в том числе после сбоя или рестарта.
Подписи и заменённые вложения проходят прежние media workers. Правка во время
анализа остаётся отдельным pending-событием следующего пакета. Рик по-прежнему
доставляет ответы только владельцу; owner mention/reply и ограничения Сали
сохраняются. Редактирование команды не запускает команду/рестарт Сали.
При первом запуске миграция сохраняет все строки, ID, индексы и расширяет
уникальность сообщения до `(chat_id,message_id,edit_update_id)`.
