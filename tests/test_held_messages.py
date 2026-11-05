"""Messages sent while Mabel researches: instant busy line, held, then answered once she's back."""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "jobs.db"}')
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, 'SessionLocal', factory)
    with factory() as db:
        db.add(database.Session(id='chat', owner='alice', name='chat', model='test', endpoint_url='http://model.test'))
        db.commit()
    yield factory
    engine.dispose()


def _researching(monkeypatch, running, subject='Yoshifumi Kondo'):
    from src.curiosity import held
    monkeypatch.setattr(held, 'research_subject', lambda: (running, subject if running else None))


def test_messages_to_the_same_chat_are_held_together(store, monkeypatch):
    from src.curiosity import held
    first = held.hold('chat', 'alice', 'hey, you around?')
    second = held.hold('chat', 'alice', 'also, did you eat?')
    assert first == second
    with store() as db:
        job = db.get(database.BackgroundToolJob, first)
        assert job.tool == 'held_message' and job.query == 'hey, you around?\n\nalso, did you eat?'
    with pytest.raises(ValueError):
        held.hold('chat', 'bob', 'not my chat')


def test_busy_line_names_the_topic():
    import random
    from src.curiosity.held import busy_line
    line = busy_line('Yoshifumi Kondo', random.Random(0))
    assert 'Yoshifumi Kondo' in line and ('promise' in line.lower() or 'back' in line.lower())
    assert '{about}' not in busy_line(None)


@pytest.mark.asyncio
async def test_held_message_is_answered_after_research_and_retried_on_failure(store, monkeypatch):
    from src.background_tool_jobs import BackgroundToolJobs
    from src.curiosity import held
    _researching(monkeypatch, True)
    job_id = held.hold('chat', 'alice', 'how was your day?')
    attempts = []

    async def writer(session, payload):
        attempts.append(payload['query'])
        if len(attempts) == 1:
            raise TimeoutError('model busy')
        return "I'm back! My day was mostly Ghibli trivia. Yours?"
    jobs = BackgroundToolJobs(is_busy=lambda sid: False, resolvers={held.TOOL: held.ready_to_answer},
                              writers={held.TOOL: writer})
    await jobs.tick()
    assert jobs.list_for_chat('chat', 'alice')[0]['status'] == 'running', 'held while she researches'
    _researching(monkeypatch, False)
    await jobs.tick()
    assert attempts == ['how was your day?'] and jobs.list_for_chat('chat', 'alice')[0]['status'] == 'ready'
    jobs._writer_failures[job_id] = (1, 0.0)            # skip the backoff wait
    await jobs.tick()
    delivered = jobs.list_for_chat('chat', 'alice')[0]
    assert delivered['status'] == 'delivered'
    assert delivered['message']['content'] == "I'm back! My day was mostly Ghibli trivia. Yours?"
    with store() as db:
        meta = json.loads(db.get(database.ChatMessage, delivered['message']['metadata']['_db_id']).meta_data)
    assert 'background_tool_result' not in meta and meta['background_tool'] == 'held_message'


def test_chat_route_holds_only_plain_messages_to_mabel_while_researching(monkeypatch):
    from types import SimpleNamespace
    from routes import chat_routes
    from src.curiosity import held
    saved, holds = [], []

    class Sess:
        owner, model = 'alice', 'gyro'
        def add_message(self, message):
            saved.append(message)
    monkeypatch.setattr(held, 'hold', lambda sid, owner, msg: holds.append((sid, owner, msg)) or 'held-1')
    mabel = SimpleNamespace(validate_and_extract_preset=lambda pid: (0.6, 0, 'p', 'M.A.B.E.L.', '', 'general'))
    other = SimpleNamespace(validate_and_extract_preset=lambda pid: (0.6, 0, 'p', 'Coder', '', 'general'))
    request = SimpleNamespace()
    call = lambda handler, msg='hey', **kw: chat_routes._maybe_hold_for_research(
        request, Sess(), 'chat', msg, 'p', handler, compare_mode=kw.get('compare', False),
        incognito=False, approval=kw.get('approval', False), has_attachments=False)
    _researching(monkeypatch, False)
    assert call(mabel) is None
    _researching(monkeypatch, True)
    assert call(other) is None and call(mabel, approval=True) is None and call(mabel, compare=True) is None
    assert call(mabel, msg='   ') is None and not holds
    response = call(mabel, msg='you there?')
    assert response is not None and holds == [('chat', 'alice', 'you there?')]
    assert [m.role for m in saved] == ['user', 'assistant'] and saved[0].content == 'you there?'
    assert 'Yoshifumi Kondo' in saved[1].content and saved[1].metadata['held_busy_reply']
