from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class SalesSettings:
    token: str = field(repr=False)
    chat_id: int
    runtime: Path
    codex_home: Path
    model: str
    effort: str
    system_topic_id: int = 0

    @classmethod
    def load(cls, root: Path, env_file: Path | None = None):
        values = {**dotenv_values(env_file or root / '.env'), **os.environ}
        token = str(values.get('TELEGRAM_SALES_BADDY_BOT_TOKEN') or '').strip()
        chat_id = int(values.get('OWNER_SALES_BADDY_CHAT_ID') or '0')
        if not token or chat_id >= 0:
            raise ValueError('Configure TELEGRAM_SALES_BADDY_BOT_TOKEN and OWNER_SALES_BADDY_CHAT_ID (forum supergroup)')
        if token == values.get('TELEGRAM_BOT_TOKEN'):
            raise ValueError('Sales Buddy must use a separate Telegram token')
        runtime = Path(values.get('SALES_BUDDY_RUNTIME_DIR') or root / 'runtime' / 'sales_buddy').resolve()
        codex_home = Path(values.get('SALES_BUDDY_CODEX_HOME') or runtime / 'codex').resolve()
        # Runtime overrides cannot accidentally point at Rick's existing state.
        if runtime == (root / 'runtime').resolve() or not codex_home.is_relative_to(runtime):
            raise ValueError('Sales Buddy requires its own runtime and Codex home')
        if (runtime / 'agentbridge.sqlite3').exists() or runtime == root.resolve():
            raise ValueError('Sales Buddy runtime overlaps Rick')
        if runtime.is_relative_to(root.resolve()) and not runtime.is_relative_to(root / 'runtime' / 'sales_buddy'):
            raise ValueError('In this checkout, sales runtime must be under runtime/sales_buddy')
        system_topic_id = int(values.get('SALES_BUDDY_SYSTEM_TOPIC_ID') or '0')
        if system_topic_id < 0:
            raise ValueError('Sales system topic must be zero (General) or a positive topic ID')
        return cls(token, chat_id, runtime, codex_home,
                   str(values.get('OWNER_SALES_BADDY_CODEX_MODEL') or 'gpt-6-luna'),
                   str(values.get('OWNER_SALES_BADDY_CODEX_REASONING_EFFORT') or 'xhigh'), system_topic_id)
