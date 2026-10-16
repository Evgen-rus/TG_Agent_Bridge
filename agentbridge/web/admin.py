"""Owner bootstrap. The invitation is never printed or logged."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from .store import WebStore


async def bootstrap(root, send_owner):
    from agentbridge.settings import Settings
    from telegram import Bot
    settings = Settings.from_env(root)
    origin = os.environ['AGENTBRIDGE_WEB_ORIGIN']
    store = WebStore(root/'runtime/web/access.sqlite3')
    token = store.invite('evgen',[])
    message = f'Веб-приложения готовы к первому входу. Rick: {origin}/rick/\nSally: {origin}/sally/\n\nОдноразовая ссылка (24 часа): {origin}/rick/#invite={token}\nСоздайте пароль. Логин: evgen. Он подходит для обоих приложений, входы отдельные. Приглашения Дмитрию и Сергею — в разделе «Ещё».'
    path = root/'runtime/web/owner-invitation.txt'
    descriptor = os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as output:
        output.write(message)
    if send_owner:
        async with Bot(settings.telegram_bot_token) as bot:
            await bot.send_message(chat_id=settings.owner_chat_id,text=message,protect_content=True)
        print('Owner invitation delivered; protected recovery file retained.')
    else:
        print('Owner invitation saved to runtime/web/owner-invitation.txt (private).')


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--send-owner',action='store_true')
    args = parser.parse_args()
    try:
        asyncio.run(bootstrap(Path.cwd(),args.send_owner))
    except Exception as exc:
        raise SystemExit('Bootstrap failed: '+type(exc).__name__) from None
