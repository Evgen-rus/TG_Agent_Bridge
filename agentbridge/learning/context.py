"""Optional Wiki context. A failed lookup must never stop the normal bot flow."""
import logging
import os

logger = logging.getLogger(__name__)


def load_store(path):
    from .store import LearningStore
    try:
        return LearningStore(path)
    except Exception as exc:
        logger.warning('event=wiki_start_failed error_type=%s',type(exc).__name__)
        return None


def enabled(product):
    return os.getenv(product.upper()+'_WIKI_ENABLED','false').casefold()=='true'


def prepare(store, product, query, context, thread_id):
    if not store:
        return '', False, []
    try:
        if not enabled(product):
            return '', store.thread_has_rules(thread_id), []
        return store.context_for_thread(query,str(context),thread_id)
    except Exception as exc:
        logger.warning('event=wiki_lookup_failed product=%s error_type=%s',product,type(exc).__name__)
        return '', False, []


def remember(store, product, thread_id, ids):
    if store and enabled(product) and thread_id:
        try:
            store.remember_thread(thread_id,ids)
        except Exception as exc:
            logger.warning('event=wiki_thread_save_failed product=%s error_type=%s',product,type(exc).__name__)
