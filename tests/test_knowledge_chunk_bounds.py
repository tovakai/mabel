"""Regression tests for oversized Markdown lines and incremental vector preservation."""
import json
import struct

from src.knowledge_engine.markdown import parse
from src.knowledge_engine.index import VaultIndex


def test_chat_transcript_long_line_is_split_losslessly_with_source_evidence():
    long_line = 'conversation-codepoint-with-markers ' * 1300
    text = '# Transcript\n' + long_line + '\n## Later\nend of transcript\n'
    chunks = parse(text, maximum=3200, minimum=600)['chunks']
    assert len(chunks) > 12
    assert all(len(c['text']) <= 1600 for c in chunks)
    fragments = [c for c in chunks if c['start_line'] == 2 and c['end_line'] == 2]
    assert len(fragments) > 12
    assert ''.join(c['text'] for c in fragments) == long_line
    assert chunks[-1]['end_line'] == 4
    assert any('end of transcript' in c['text'] for c in chunks)


def test_long_unbroken_token_is_split_even_without_word_boundaries():
    text = 'x' * 37000
    chunks = parse(text, maximum=3200)['chunks']
    assert ''.join(c['text'] for c in chunks) == text
    assert len(chunks) > 20
    assert all(c['start_line'] == c['end_line'] == 1 for c in chunks)
    assert max(len(c['text']) for c in chunks) <= 1600


def test_normal_document_retains_existing_chunk_text_and_lines():
    text = '# Head\nNormal sentence.\n\n## Another\nMore text.\n'
    chunks = parse(text)['chunks']
    assert len(chunks) == 2
    assert chunks[0]['text'] == '# Head\nNormal sentence.\n'
    assert chunks[0]['start_line'] == 1 and chunks[0]['end_line'] == 3
    assert chunks[1]['text'] == '## Another\nMore text.'
    assert chunks[1]['start_line'] == 4 and chunks[1]['end_line'] == 5


def test_index_rechunks_legacy_oversize_note_without_losing_other_vectors(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    chat = '# Imported chat\n' + 'semantic long line ' * 2100 + '\n'
    (vault / 'Long.md').write_text(chat, encoding='utf-8')
    (vault / 'Short.md').write_text('# Short\nPreserved vector contents\n', encoding='utf-8')
    dbindex = VaultIndex(vault, owner='alice', cache_dir=tmp_path / 'cache')
    dbindex.reconcile()
    with dbindex.connect() as db:
        short = db.execute("SELECT c.id,c.content_hash FROM chunks c JOIN notes n ON c.note_id=n.id WHERE n.path='Short.md'").fetchone()
        db.execute("INSERT INTO embedding_spaces VALUES ('space','model','rev',3,'l2')")
        db.execute("INSERT INTO chunk_vectors VALUES (?,?,?,?)", (short['id'], 'space', short['content_hash'], struct.pack('<fff',1.,0.,0.)))
        # Simulate a pre-fix index containing one enormous source line.
        db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT c.id FROM chunks c JOIN notes n ON n.id=c.note_id WHERE n.path='Long.md')")
        db.execute("DELETE FROM chunks WHERE note_id=(SELECT id FROM notes WHERE path='Long.md')")
        db.execute("INSERT INTO chunks VALUES ('oldbad',(SELECT id FROM notes WHERE path='Long.md'),'[]',1,2,?,'hash')", (chat,))
        dbindex.set_meta(db, 'chunk_config', json.dumps([150,800,[]]))
    before = {p.name:p.read_bytes() for p in vault.glob('*.md')}
    result = dbindex.reconcile()
    assert result['changed'] == 2
    assert {p.name:p.read_bytes() for p in vault.glob('*.md')} == before
    with dbindex.connect() as db:
        long_chunks = db.execute("SELECT c.* FROM chunks c JOIN notes n ON n.id=c.note_id WHERE n.path='Long.md' ORDER BY start_line,c.id").fetchall()
        assert len(long_chunks) > 12
        assert all(len(r['text']) <= 1600 for r in long_chunks)
        assert all(r['start_line'] <= r['end_line'] for r in long_chunks)
        assert not db.execute("SELECT 1 FROM chunks WHERE id='oldbad'").fetchone()
        assert db.execute("SELECT count(*) FROM chunk_vectors WHERE chunk_id=?",(short['id'],)).fetchone()[0] == 1
    assert dbindex.reconcile()['changed'] == 0
