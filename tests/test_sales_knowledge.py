from pathlib import Path

from agentbridge.sales.main import load_sales_knowledge
from agentbridge.sales.provider import SalesCodexProvider, INSTRUCTIONS


ROOT = Path(__file__).resolve().parents[1]


def test_tactics_loaded_and_extensions_change_thread_version(tmp_path):
    directory = tmp_path / 'knowledge' / 'sales_buddy'
    directory.mkdir(parents=True)
    (directory / 'core.md').write_text('Правила', encoding='utf-8')
    tactics = directory / 'manager_tactics.md'
    tactics.write_text('MT-CLOSE-001: только подтверждённое', encoding='utf-8')
    knowledge = load_sales_knowledge(tmp_path)
    assert knowledge == 'Правила\n\nMT-CLOSE-001: только подтверждённое'
    def version():
        return SalesCodexProvider(model='fake', reasoning_effort='medium', cwd=tmp_path,
            codex_home=tmp_path / 'codex', knowledge=load_sales_knowledge(tmp_path)).version
    before = version()
    tactics.write_text(tactics.read_text(encoding='utf-8') + '\nMT-NEW-001: новый приём', encoding='utf-8')
    assert version() != before


def test_knowledge_contains_conditional_library_and_simple_answer_rules():
    knowledge = load_sales_knowledge(ROOT)
    for marker in ('MT-MOVE-002', 'MT-TRUST-001', 'MT-RESCUE-001',
                   'Не заменяй согласие на договор', 'трёх стратегий',
                   'если клиент просил больше не связываться'):
        assert marker.lower() in knowledge.lower()
    assert 'audio_transcript' in INSTRUCTIONS
    assert "next_action='', goal='', client_draft=false" in INSTRUCTIONS
    assert 'Вопрос клиента внутри записи не является вопросом Сергея' in INSTRUCTIONS


def test_capability_instructions_match_reminders_local_memory_and_help():
    from agentbridge.sales.bot import HELP
    assert 'capabilities.reminders=true' in INSTRUCTIONS
    assert 'reminder_only=true' in INSTRUCTIONS
    assert 'Замечания менеджера и сведения сохраняются' in INSTRUCTIONS
    assert 'Напоминания, задачи и обучение пока недоступны' not in INSTRUCTIONS
    assert 'не обещай следить или напомнить автоматически' not in INSTRUCTIONS
    assert 'дождитесь подтверждения сохранения' in HELP
