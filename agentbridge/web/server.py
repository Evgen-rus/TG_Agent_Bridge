from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from .store import WebError

logger = logging.getLogger(__name__)


def make_server(root: Path, store, data, origin, port=18082):
    from .push import PushService
    pushes = {product:PushService(store,product=product) for product in ('rick','sally')}
    parsed_origin = urlsplit(origin)
    if parsed_origin.scheme != 'https' or not parsed_origin.netloc or parsed_origin.path:
        raise ValueError('AGENTBRIDGE_WEB_ORIGIN must be an HTTPS origin without a path')
    dist = (root / 'mobile-concepts/dist').resolve()
    if not (dist/'index.html').is_file():
        raise ValueError('Build mobile-concepts before enabling Web UI')

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False
        slots = threading.BoundedSemaphore(16)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                request.close()
                return
            super().process_request(request, client_address)

        def process_request_thread(self, request, client_address):
            try:
                request.settimeout(10)
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'AgentBridge'

        def log_message(self, *args):
            pass  # URLs may carry legacy invite links; never log request bodies or cookies.

        def send(self, status, body, mime='application/json; charset=utf-8', cookie=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Permissions-Policy', 'camera=(), microphone=(self), geolocation=()')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.send_header('Connection','close')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.handle_request(False)

        def do_POST(self):
            self.handle_request(True)

        def handle_request(self, mutation):
            try:
                path = unquote(urlsplit(self.path).path)
                parts = path.split('/')
                product = parts[1] if len(parts)>1 else ''
                if product not in ('rick','sally'):
                    raise WebError(404,'Страница не найдена')
                push = pushes[product]
                cookie_name = '__Secure-'+product+'_sid'
                cookies = SimpleCookie()
                cookies.load(self.headers.get('Cookie',''))
                token = cookies[cookie_name].value if cookie_name in cookies else ''
                payload = {}
                endpoint = '/'.join(parts[2:])
                binary = endpoint == 'api/upload'
                if mutation:
                    if self.headers.get('Origin') != origin:
                        raise WebError(403,'Источник запроса не разрешён')
                    if not binary and self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                        raise WebError(415,'Ожидается JSON')
                    length = int(self.headers.get('Content-Length','0'))
                    if not 0 < length <= (20*1024*1024 if binary else 32768):
                        raise WebError(413,'Слишком большой запрос')
                    payload = {} if binary else json.loads(self.rfile.read(length))
                    if not isinstance(payload,dict):
                        raise WebError(400,'Некорректный запрос')
                ip = self.headers.get('X-Real-IP') or self.client_address[0]
                if endpoint == 'api/login' and mutation:
                    login, password = payload.get('login',''), payload.get('password','')
                    if not isinstance(login,str) or len(login)>40 or not isinstance(password,str) or len(password)>128:
                        raise WebError(400,'Некорректные данные')
                    session = store.login(login,password,product,ip)
                    return self.send(200,{'ok':True},cookie=f'{cookie_name}={session}; Path=/{product}/; HttpOnly; Secure; SameSite=Strict; Max-Age=604800')
                if endpoint == 'api/enroll' and mutation:
                    store.throttle('enroll:'+ip)
                    invite = payload.get('token','')
                    if not isinstance(invite,str) or len(invite)>100:
                        raise WebError(400,'Некорректное приглашение')
                    login = store.enroll(invite,payload.get('password',''))
                    return self.send(200,{'login':login})
                if endpoint.startswith('api/'):
                    user = store.session(token,product)
                    if mutation and not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),user['csrf']):
                        raise WebError(403,'Обновите страницу: проверка сессии не пройдена')
                    if endpoint == 'api/session' and not mutation:
                        return self.send(200,{k:user[k] for k in ('name','login','role','csrf')})
                    if endpoint == 'api/logout' and mutation:
                        store.logout(token)
                        return self.send(200,{'ok':True},cookie=f'{cookie_name}=; Path=/{product}/; Secure; HttpOnly; SameSite=Strict; Max-Age=0')
                    if endpoint == 'api/contexts' and not mutation:
                        return self.send(200,data.contexts(user,product))
                    if endpoint == 'api/memory':
                        from .learning import memory
                        return self.send(200,memory(data,user,product,payload if mutation else None))
                    if endpoint == 'api/presence' and mutation:
                        return self.send(200,push.presence(user,token,payload.get('visible'),payload.get('window_id','main')))
                    if endpoint == 'api/notifications' and not mutation:
                        after = float(parse_qs(urlsplit(self.path).query).get('after',['0'])[0])
                        if not math.isfinite(after) or after<0:
                            raise WebError(400,'Некорректное время')
                        result = data.notifications(user,product,after)
                        push.acknowledge(token)
                        return self.send(200,result)
                    if endpoint == 'api/push-config' and not mutation:
                        return self.send(200,push.config(user,token))
                    if endpoint == 'api/push-subscribe' and mutation:
                        store.throttle('push:'+str(user['id']),limit=10,seconds=60)
                        return self.send(200,push.subscribe(user,token,payload.get('subscription')))
                    if endpoint == 'api/push-unsubscribe' and mutation:
                        return self.send(200,push.unsubscribe(user,token))
                    if endpoint == 'api/telegram' and not mutation:
                        chat = data.owner_chat if product=='rick' and user['role']=='owner' else data.sales_chat if product=='sally' else None
                        return self.send(200,{'url':data.telegram_url(chat) if chat else ''})
                    if endpoint == 'api/reminder' and not mutation:
                        ident = int(parse_qs(urlsplit(self.path).query).get('id',['0'])[0])
                        return self.send(200,data.reminder(user,ident,product))
                    if endpoint == 'api/invite-contexts' and not mutation and user['role']=='owner':
                        target = parse_qs(urlsplit(self.path).query).get('product',[''])[0]
                        if target not in ('rick','sally'):
                            raise WebError(400,'Выберите приложение')
                        return self.send(200,data.contexts(user,target))
                    if endpoint == 'api/invite' and mutation and user['role']=='owner':
                        login = payload.get('login')
                        target = {'dmitry':'rick','sergey':'sally'}.get(login)
                        if not target:
                            raise WebError(400,'Выберите пользователя')
                        invite = store.invite(login,[])
                        return self.send(200,{'url':f'{origin}/{target}/#invite={invite}'})
                    if endpoint == 'api/revoke' and mutation and user['role']=='owner':
                        if payload.get('login') not in ('dmitry','sergey'):
                            raise WebError(400,'Недопустимый пользователь')
                        with store.connect() as db:
                            db.execute('UPDATE users SET disabled=1 WHERE login=?',(payload['login'],))
                            db.execute('UPDATE invites SET used=1 WHERE login=?',(payload['login'],))
                            db.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE login=?)',(payload['login'],))
                        return self.send(200,{'ok':True})
                    query = parse_qs(urlsplit(self.path).query)
                    context = str(payload.get('context','')) if mutation and not binary else query.get('context',[''])[0]
                    data.context(user,product,context)
                    if binary and mutation:
                        from .media import upload
                        return self.send(201,upload(store,user,product,context,
                            query.get('filename',['file'])[0],self.rfile,length))
                    if endpoint == 'api/media' and not mutation:
                        ident = query.get('id',[''])[0]
                        from agentbridge.retention import EXPIRED_MESSAGE, QUOTA_MESSAGE, media_lock, RETENTION_SECONDS
                        with media_lock(store.path.parent,shared=True), store.connect() as db:
                            db.execute('BEGIN IMMEDIATE')
                            row = db.execute('SELECT * FROM media WHERE id=? AND product=? AND context=?', (ident,product,context)).fetchone()
                            if not row:
                                raise WebError(404,'Вложение недоступно')
                            state = db.execute('SELECT state FROM jobs WHERE id=?',(row['job_id'],)).fetchone()
                            expired = row['created'] < time.time()-RETENTION_SECONDS and (row['job_id'] is None or (state and state['state']=='done'))
                            if row['removed'] or expired:
                                reason = row['removed'] or 'expired'
                                db.execute('UPDATE media SET removed=?,requested=CASE WHEN requested=2 THEN 2 ELSE 0 END WHERE id=?',(reason,ident))
                                db.commit()
                                raise WebError(410,EXPIRED_MESSAGE if reason=='expired' else QUOTA_MESSAGE)
                            file = Path(row['path'])
                            from agentbridge.retention import safe_path
                            file = safe_path(file,store.path.parent/'media')
                            missing = file is None or not file.is_file()
                            if missing:
                                db.execute('UPDATE media SET requested=1 WHERE id=? AND requested=0', (ident,))
                            else:
                                content = file.read_bytes()
                        # Commit the download request before returning an HTTP error.
                        if missing:
                            raise WebError(409,'Загружаем вложение из Telegram. Повторите через несколько секунд.')
                        return self.send(200,content,row['mime'])
                    if endpoint == 'api/detail' and not mutation:
                        return self.send(200,data.detail(user,product,context))
                    if endpoint == 'api/history' and not mutation:
                        return self.send(200,store.history(user,product,context))
                    if endpoint == 'api/reminder-action' and mutation:
                        job = store.reminder_action(user,context,payload.get('job_id'),payload.get('revision'),
                                                    payload.get('action'),payload.get('text',''),product=product)
                        return self.send(202,{'id':job})
                    if endpoint == 'api/ask' and mutation:
                        store.throttle('ask:'+str(user['id']),limit=30,seconds=60)
                        text = payload.get('text','')
                        ids = payload.get('attachments',[])
                        attachments = store.media(user,product,context,ids)
                        if not isinstance(text,str) or len(text.strip())>6000 or not (text.strip() or attachments):
                            raise WebError(400,'Введите сообщение до 6000 символов')
                        reply = payload.get('reply_to')
                        if reply is not None:
                            if type(reply) is not int or product != 'sally':
                                raise WebError(400,'Недопустимый ответ')
                            data.reply_target(context,reply)
                        command = {'text':text.strip()}
                        if ids:
                            command['attachments'] = ids
                        if reply is not None:
                            command['reply_to'] = reply
                        job = store.enqueue(user,product,context,self.headers.get('Idempotency-Key',''),command)
                        return self.send(202,{'id':job})
                    raise WebError(404,'Действие недоступно')
                if mutation:
                    raise WebError(404,'Действие недоступно')
                relative = '/'.join(parts[2:]) or 'index.html'
                if relative == 'manifest.webmanifest':
                    icons = [dict(src=f'/{product}/{product}-v2-{size}.png',sizes=f'{size}x{size}',type='image/png',purpose='any') for size in (192,512)]
                    return self.send(200,dict(id=f'/{product}/',name='Rick' if product=='rick' else 'Sally',short_name=product.title(),
                        start_url=f'/{product}/',scope=f'/{product}/',display='standalone',background_color='#101218' if product=='rick' else '#f6f7f3',
                        theme_color='#101218' if product=='rick' else '#f6f7f3',icons=icons),'application/manifest+json')
                if relative in (f'{product}-v2-180.png',f'{product}-v2-192.png',f'{product}-v2-512.png','icon.png'):
                    file = root/'agentbridge/web/icons'/(f'{product}-v2-512.png' if relative=='icon.png' else relative)
                elif relative == 'icon.svg':
                    file = root/f'Rick_Sally_Concept_Kit/design/{product}-icon.svg'
                else:
                    file = (dist/relative).resolve()
                    if not file.is_relative_to(dist):
                        raise WebError(404,'Файл не найден')
                if not file.is_file():
                    raise WebError(404,'Файл не найден')
                return self.send(200,file.read_bytes(),mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
            except WebError as exc:
                self.send(exc.status,{'error':exc.message})
            except (ValueError,TypeError,UnicodeError):
                self.send(400,{'error':'Некорректный запрос'})
            except (BrokenPipeError,ConnectionResetError,TimeoutError):
                pass
            except Exception as exc:
                logger.warning('event=web_request_failed error_type=%s',type(exc).__name__)
                self.send(503,{'error':'Сервис временно недоступен. Попробуйте позже.'})

    return Server(('127.0.0.1',port),Handler)


def start_server(root,store,data):
    server = make_server(root,store,data,os.environ['AGENTBRIDGE_WEB_ORIGIN'],int(os.getenv('AGENTBRIDGE_WEB_PORT','18082')))
    threading.Thread(target=server.serve_forever,daemon=True,name='web-http').start()
    return server
