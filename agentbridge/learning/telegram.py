"""Wiki notifications and review, shared by the existing Telegram workers."""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlsplit

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)
CALLBACK = r'^wiki:(accept|reject|refine|disable):(\d+):(\d+)$'


def candidate_message(row):
    # Never attach acceptance buttons to a silently shortened proposed rule.
    text = '\n\n'.join((f"Память · {row['title']}",
        'Когда применять: ' + row.get('situation', ''),
        'Что делать иначе: ' + row.get('behavior', ''),
        'Основание: ' + row.get('rationale', ''),
        'Область: ' + ('Все обсуждения' if row.get('scope') == 'global'
            else 'Обсуждение ' + str(row.get('context', '')))))
    for source in row.get('sources', []):
        url = source.get('url', '')
        if urlsplit(url).scheme not in {'https', 'http', 'tg'}:
            url = ''
        evidence = '\n\nИсточник: ' + source.get('text', '')[:350]
        if url:
            evidence += '\n' + url
        text += evidence
    return text


def candidate_keyboard(row):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label,
        callback_data=f"wiki:{action}:{row['id']}:{row['version']}")
        for label, action in (('Принять', 'accept'), ('Уточнить', 'refine'), ('Отклонить', 'reject'))]])


async def deliver_notifications(store, bot, chat_id, topic_id=0):
    """Run inside the existing retry worker; persist success only after send."""
    from agentbridge.telegram.formatter import split_owner_message
    try:
        notifications = await asyncio.to_thread(store.pending_notifications)
    except Exception as exc:
        logger.warning('event=wiki_queue_failed error_type=%s',type(exc).__name__)
        return
    for item in notifications:
        row = item['payload']
        try:
            parts = split_owner_message(candidate_message(row))
            for index, part in enumerate(parts):
                sent = await bot.send_message(chat_id=chat_id,
                    message_thread_id=topic_id or None, text=part,
                    reply_markup=candidate_keyboard(row) if index == len(parts)-1 else None)
            await asyncio.to_thread(store.notification_delivered, item['id'], sent.message_id)
        except Exception as exc:
            logger.warning('event=wiki_delivery_pending error_type=%s', type(exc).__name__)
            break


async def review_callback(store, update, context, allowed):
    query, user = update.callback_query, update.effective_user
    match = re.fullmatch(CALLBACK, query.data or '')
    if not allowed or not match or not user or user.is_bot or not query.message:
        await query.answer('Подтверждение недоступно.', show_alert=True)
        return
    action, candidate_id, version = match[1], int(match[2]), int(match[3])
    try:
        row = await asyncio.to_thread(store.get, candidate_id)
    except KeyError:
        await query.answer('Запись не найдена.')
        return
    if not row or row['version'] != version or (action != 'disable' and row['status'] != 'pending'):
        await query.answer('Запись изменилась или решение уже принято.')
        return
    if action == 'refine':
        await query.answer()
        sent = await context.bot.send_message(chat_id=update.effective_chat.id,
            message_thread_id=getattr(query.message, 'message_thread_id', None),
            text='Ответьте на это сообщение новой формулировкой правила «Что делать иначе» (до 1500 символов). После уточнения его нужно принять отдельно.',
            reply_markup=ForceReply(selective=True))
        await asyncio.to_thread(store.save_prompt, update.effective_chat.id,
            sent.message_id, candidate_id, version)
        return
    try:
        await asyncio.to_thread(store.decide, candidate_id, action, str(user.id), expected_version=version)
    except (ValueError, KeyError):
        await query.answer('Запись изменилась или решение уже принято.')
        return
    await query.answer('Решение сохранено.')
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as exc:
        logger.warning('event=wiki_markup_failed error_type=%s', type(exc).__name__)


async def refinement_reply(store, update, context, allowed):
    message, user, chat = update.effective_message, update.effective_user, update.effective_chat
    reply = getattr(message, 'reply_to_message', None)
    if not store or not allowed or not user or user.is_bot or not chat or not reply:
        return False
    prompt = await asyncio.to_thread(store.prompt, chat.id, reply.message_id)
    if not prompt:
        return False
    text = (getattr(message, 'text', None) or '').strip()
    if not text or len(text) > 1500:
        await context.bot.send_message(chat_id=chat.id, text='Напишите новую формулировку текстом, до 1500 символов.')
        return True
    try:
        await asyncio.to_thread(store.decide, prompt['candidate_id'], 'refine', str(user.id),
            comment=text, expected_version=prompt['version'], changes={'behavior': text})
    except (ValueError, KeyError):
        await context.bot.send_message(chat_id=chat.id, text='Запись уже изменилась. Откройте актуальный кандидат.')
    return True
