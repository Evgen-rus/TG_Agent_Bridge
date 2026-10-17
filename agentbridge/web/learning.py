"""Product-wide memory operations shared by both authenticated web apps."""
from agentbridge.learning.store import LearningStore
from .store import WebError, WebStore


def memory(data, user, product, payload=None):
    if not WebStore.allowed(user, product):
        raise WebError(403, 'Нет доступа к памяти этого приложения')
    store = LearningStore(data.rick_path if product == 'rick' else data.sales_path)
    if payload is not None:
        ident, version = payload.get('id'), payload.get('expected_version')
        action, comment = payload.get('action'), payload.get('comment', '')
        if (type(ident) is not int or ident < 1 or type(version) is not int or version < 1
                or action not in ('accept', 'reject', 'refine', 'disable')
                or not isinstance(comment, str) or len(comment) > 4000):
            raise WebError(400, 'Некорректное решение или версия правила')
        changes = {key: payload[key] for key in ('title', 'situation', 'behavior', 'rationale') if key in payload}
        if any(not isinstance(value, str) or not value.strip() or len(value) > 1500 for value in changes.values()):
            raise WebError(400, 'Заполните текст правила, не более 1500 символов на поле')
        if changes and action != 'refine':
            raise WebError(400, 'Текст меняется только через уточнение')
        try:
            store.decide(ident, action, user['name'], comment=comment,
                         expected_version=version, changes=changes or None)
        except KeyError:
            raise WebError(404, 'Правило не найдено') from None
        except ValueError:
            raise WebError(409, 'Правило изменилось или действие недоступно. Обновите память.') from None
    return store.snapshot()
