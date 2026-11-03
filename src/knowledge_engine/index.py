"""Disposable SQLite/FTS5 index. Markdown is never written by this module."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import asdict
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import sqlite3
from uuid import uuid4

from src.agent_runtime.resources import FilesystemRoot, FilesystemResource
from src.agent_runtime.workspace_discovery import is_control_directory
from .markdown import parse, digest, json_value, RELATIONS

SCHEMA = 3
INDEXER = 'obsidian-1'
MAX_NOTE_BYTES = 2 * 1024 * 1024
MAX_NOTES = 50000


def filesystem_id(value):
    """Encode a filesystem integer losslessly as SQLite TEXT.

    SQLite INTEGER parameters are limited to signed 64 bits, whereas Windows
    volume/file identifiers can be unsigned or wider.  The nonnumeric prefix
    prevents columns with legacy INTEGER affinity from converting the string
    back into an integer or lossy REAL.
    """
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError('Filesystem identity must be an integer or None')
    return f'fs:{value}'


def stored_filesystem_id(value):
    """Normalize both legacy signed SQLite integers and new TEXT identities."""
    if value is None:
        return None
    if type(value) is int:
        return filesystem_id(value)
    if isinstance(value, str) and re.fullmatch(r'fs:-?[0-9]+', value):
        return value
    raise ValueError('Unrecognized stored filesystem identity')


def now():
    return datetime.now(timezone.utc).isoformat()


def local_cache():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'Mabel/knowledge'
    return Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'mabel/knowledge'


def relative(value, *, empty=True):
    if not isinstance(value, str) or '\0' in value or '\\' in value:
        raise ValueError('Use a vault-relative POSIX path')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or ':' in value or (not empty and not value):
        raise ValueError('Path escapes the vault')
    return '' if str(path) == '.' else path.as_posix()


class VaultIndex:
    def __init__(self, root, *, owner='', cache_dir=None, exclusions=(), inclusions=(), minimum_tokens=150, maximum_tokens=800, edge_weights=None, relationship_keys=None):
        self.root = Path(root).expanduser().resolve(strict=True)
        self.seal = FilesystemRoot.seal(str(self.root), owner=owner)
        self.owner = owner
        self.relationship_keys = frozenset(RELATIONS if relationship_keys is None else relationship_keys)
        self.edge_weights = edge_weights or {'property':3.,'inline':2.,'embed':1.}
        if set(self.edge_weights) != {'property','inline','embed'} or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<v<=100 for v in self.edge_weights.values()):
            raise ValueError('Invalid graph weights')
        if type(minimum_tokens) is not int or type(maximum_tokens) is not int or not 16 <= minimum_tokens <= maximum_tokens <= 4096:
            raise ValueError('Invalid chunk token targets')
        self.minimum_tokens, self.maximum_tokens = minimum_tokens, maximum_tokens
        self.exclusions = {relative(p, empty=False).casefold() for p in exclusions}
        self.inclusions = {relative(p, empty=False).casefold() for p in inclusions}
        cache = Path(cache_dir or local_cache()).expanduser().resolve()
        if cache.is_relative_to(self.root):
            raise ValueError('Index/cache must be outside the Markdown vault')
        if any((parent / '.stfolder').exists() for parent in (cache, *cache.parents)):
            raise ValueError('Index/cache must be outside a Syncthing-managed directory')
        if any((parent / '.git/HEAD').is_file() or (parent / '.git').is_file()
                for parent in (cache, *cache.parents)):
            raise ValueError('Index/cache must be outside Git working trees')
        self.vault_id = digest(owner + '\0' + str(self.root))
        folder = cache / self.vault_id
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.is_symlink() or folder.resolve() != folder:
            raise ValueError('Index directory cannot be a symlink')
        self.path = folder / 'vault_index.db'
        if self.path.is_symlink():
            raise ValueError('Index database cannot be a symlink')
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, SCHEMA):
                raise ValueError('Unsupported knowledge index schema; rebuild explicitly')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notes(
                    id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, title TEXT NOT NULL, aliases TEXT NOT NULL,
                    frontmatter TEXT NOT NULL, content_hash TEXT NOT NULL, indexed_at TEXT NOT NULL,
                    device TEXT, inode TEXT, errors TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks(
                    id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                    heading TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                    text TEXT NOT NULL, content_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS properties(
                    note_id TEXT REFERENCES notes(id) ON DELETE CASCADE, key TEXT, value TEXT,
                    PRIMARY KEY(note_id,key,value));
                CREATE INDEX IF NOT EXISTS property_value ON properties(key,value);
                CREATE TABLE IF NOT EXISTS edges(
                    id INTEGER PRIMARY KEY, source_id TEXT REFERENCES notes(id) ON DELETE CASCADE,
                    target_id TEXT REFERENCES notes(id) ON DELETE SET NULL, target TEXT NOT NULL,
                    edge_type TEXT NOT NULL, property_key TEXT NOT NULL, context TEXT NOT NULL,
                    line INTEGER NOT NULL, resolution TEXT NOT NULL DEFAULT 'unresolved');
                CREATE INDEX IF NOT EXISTS edges_target ON edges(target_id);
                CREATE INDEX IF NOT EXISTS chunks_note ON chunks(note_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED, title, aliases, heading, text, frontmatter,
                    tokenize='unicode61 remove_diacritics 2');
            """)
            if version < 2:
                # Additive migration: vector data is disposable, source notes
                # and existing internal IDs are never rewritten.
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS embedding_spaces(
                        id TEXT PRIMARY KEY,model_identity TEXT NOT NULL,revision TEXT NOT NULL,
                        dimensions INTEGER NOT NULL,normalization TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS chunk_vectors(
                        chunk_id TEXT REFERENCES chunks(id) ON DELETE CASCADE,
                        space_id TEXT REFERENCES embedding_spaces(id) ON DELETE CASCADE,
                        chunk_hash TEXT NOT NULL,embedding BLOB NOT NULL,
                        PRIMARY KEY(chunk_id,space_id));
                    CREATE INDEX IF NOT EXISTS vector_space ON chunk_vectors(space_id);
                """)
            if version < 3:
                # Schema v1/v2 stored filesystem IDs as SQLite signed integers.
                # Keep the table, note IDs, chunk IDs, edges and vectors intact:
                # SQLite accepts prefixed TEXT in legacy INTEGER-affinity columns.
                # New v3 databases declare TEXT explicitly.
                db.execute("UPDATE notes SET device='fs:' || CAST(device AS TEXT) WHERE device IS NOT NULL AND device NOT LIKE 'fs:%'")
                db.execute("UPDATE notes SET inode='fs:' || CAST(inode AS TEXT) WHERE inode IS NOT NULL AND inode NOT LIKE 'fs:%'")
            identity = json_value(asdict(self.seal.identity))
            existing = self.meta(db, 'root_identity')
            if existing and existing != identity:
                raise ValueError('Vault root identity changed; rebuild the disposable index explicitly')
            for key, value in {'schema_version':str(SCHEMA), 'indexer_version':INDEXER,
                'vault_id':self.vault_id, 'root_identity':identity}.items():
                self.set_meta(db, key, value)
            db.execute(f'PRAGMA user_version={SCHEMA}')
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        self.seal.validate()
        if self.path.is_symlink() or self.path.parent.resolve() != self.path.parent:
            raise ValueError('Index database was replaced with a symlink')
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=15000')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def meta(db, key, default=''):
        row = db.execute('SELECT value FROM index_meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def set_meta(db, key, value):
        db.execute('INSERT INTO index_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def read(self, rel):
        rel = relative(rel, empty=False)
        resource = FilesystemResource.resolve(self.seal, str(self.root / rel))
        from src.agent_runtime.effect_adapters import _read_whole
        before = os.stat(resource.path)
        snapshot = _read_whole(resource, MAX_NOTE_BYTES)
        after = os.stat(resource.path)
        if snapshot.data is None or len(snapshot.data) > MAX_NOTE_BYTES or (before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise ValueError('Source unavailable, changed or over the 2 MiB read limit')
        return snapshot.data.decode('utf-8'), resource

    def discover(self):
        found = []
        def failed(error):
            raise OSError('Vault discovery failed; existing index retained') from error
        for directory, dirs, files in os.walk(self.root, followlinks=False, onerror=failed):
            base = Path(directory)
            def allowed(name):
                path = (base / name).relative_to(self.root).as_posix().casefold()
                if path in self.exclusions or name.casefold() in self.exclusions:
                    return False
                return (not is_control_directory(name) or path in self.inclusions) and not (base / name).is_symlink()
            dirs[:] = sorted(d for d in dirs if allowed(d))
            for name in sorted(files):
                file = base / name
                if file.suffix.casefold() == '.md' and not file.is_symlink() and allowed(name):
                    found.append(file.relative_to(self.root).as_posix())
                    if len(found) > MAX_NOTES:
                        raise ValueError('Vault exceeds 50,000-note inventory budget')
        return found

    def needs_reconcile(self):
        """Cheap change probe: True when Markdown was added, removed or modified
        since the last scan began. Never reads note text or writes the index."""
        with self.connect() as db:
            started = self.meta(db, 'scan_started') or self.meta(db, 'last_scan')
            if not started:
                return True
            known = {r[0] for r in db.execute('SELECT path FROM notes')}
            try:
                known |= set(json.loads(self.meta(db, 'scan_errors') or '{}'))
            except (TypeError, ValueError):
                pass
        try:
            since = datetime.fromisoformat(started).timestamp()
        except ValueError:
            return True
        paths = self.discover()
        if set(paths) != known:
            return True
        for rel in paths:
            try:
                if os.stat(self.root / rel, follow_symlinks=False).st_mtime >= since:
                    return True
            except OSError:
                return True
        return False

    def reconcile(self):
        scan_started = now()
        paths = self.discover()
        snapshots, errors = {}, {}
        total_bytes = 0
        for rel in paths:
            try:
                text, resource = self.read(rel)
                total_bytes += len(text.encode("utf-8"))
                if total_bytes > 64 * 1024 * 1024:
                    raise MemoryError("Scan exceeds 64 MiB snapshot budget; narrow the vault root")
                snapshots[rel] = (text, digest(text), resource.identity)
            except (OSError, ValueError, UnicodeError, RuntimeError):
                errors[rel] = 'Source unreadable, changed or over budget'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            chunk_config = json_value([self.minimum_tokens,self.maximum_tokens,sorted(self.relationship_keys),'bounded-source-lines-v1'])
            rechunk = self.meta(db,'chunk_config') != chunk_config
            old = {r['path']:dict(r) for r in db.execute('SELECT * FROM notes')}
            absent = set(old) - set(paths)
            def uid(properties):
                for key in ('id','uid'):
                    value=properties.get(key)
                    if type(value) in (str,int) and value!='':
                        return key+':'+json_value(value)
                return None
            old_ids={}
            for row in old.values():
                marker=uid(json.loads(row['frontmatter']))
                if marker:
                    old_ids.setdefault(marker,[]).append(row)
            new_ids={}
            new_parsed={}
            for path,(text,_,_) in snapshots.items():
                if path not in old:
                    props=parse(text,relationship_keys=self.relationship_keys)
                    new_parsed[path]=props
                    marker=uid(props['properties'])
                    if marker:new_ids.setdefault(marker,[]).append(path)
            used, changed = set(), 0
            for rel, (text, hashed, identity) in snapshots.items():
                device_id = filesystem_id(identity.device)
                inode_id = filesystem_id(identity.inode)
                previous = old.get(rel)
                if previous is None:
                    identity_matches = [old[p] for p in absent if p not in used and
                        stored_filesystem_id(old[p]['device']) == device_id and
                        stored_filesystem_id(old[p]['inode']) == inode_id and
                        identity.inode and old[p]['content_hash'] == hashed]
                    hash_matches = [old[p] for p in absent if p not in used and old[p]['content_hash'] == hashed]
                    marker=uid(new_parsed[rel]['properties'])
                    uid_matches=old_ids.get(marker,[]) if marker and len(new_ids.get(marker,[]))==1 else []
                    uid_matches=[r for r in uid_matches if r['path'] in absent and r['path'] not in used] if len(uid_matches)==1 else []
                    candidates = uid_matches or (identity_matches if len(identity_matches) == 1 else hash_matches)
                    if len(candidates) == 1 and (uid_matches or identity_matches or sum(v[1] == hashed for p,v in snapshots.items() if p not in old) == 1):
                        previous = candidates[0]
                        used.add(previous['path'])
                        db.execute('UPDATE notes SET path=? WHERE id=?', (rel, previous['id']))
                note_id = previous['id'] if previous else uuid4().hex
                if not rechunk and previous and previous['content_hash'] == hashed and previous['path'] == rel:
                    # File replacement with identical content can change filesystem
                    # identity. Refresh it without rebuilding unchanged chunks.
                    if (stored_filesystem_id(previous['device']) != device_id or
                            stored_filesystem_id(previous['inode']) != inode_id):
                        db.execute('UPDATE notes SET device=?,inode=? WHERE id=?',
                            (device_id, inode_id, note_id))
                    continue
                parsed = parse(text,minimum=self.minimum_tokens*4,maximum=self.maximum_tokens*4,relationship_keys=self.relationship_keys)
                title = parsed['title'] or Path(rel).stem
                db.execute('DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE note_id=?)', (note_id,))
                for table in ('edges', 'properties'):
                    db.execute(f'DELETE FROM {table} WHERE {"source_id" if table == "edges" else "note_id"}=?', (note_id,))
                db.execute("""INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    path=excluded.path,title=excluded.title,aliases=excluded.aliases,frontmatter=excluded.frontmatter,
                    content_hash=excluded.content_hash,indexed_at=excluded.indexed_at,device=excluded.device,
                    inode=excluded.inode,errors=excluded.errors""",
                    (note_id, rel, title, json_value(parsed['aliases']), json_value(parsed['properties']), hashed,
                     now(), device_id, inode_id, json_value(parsed['errors'])))
                for key, value in parsed['properties'].items():
                    for item in value if isinstance(value, list) else [value]:
                        db.execute('INSERT OR IGNORE INTO properties VALUES(?,?,?)', (note_id,key,json_value(item)))
                chunk_ids = []
                occurrences = {}
                for chunk in parsed['chunks']:
                    key = json_value(chunk['heading']) + '\0' + chunk['content_hash']
                    occurrence = occurrences.get(key, 0)
                    occurrences[key] = occurrence + 1
                    cid = digest(note_id + '\0' + key + '\0' + str(occurrence))
                    chunk_ids.append(cid)
                    db.execute('''INSERT INTO chunks VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                        heading=excluded.heading,start_line=excluded.start_line,end_line=excluded.end_line,
                        text=excluded.text,content_hash=excluded.content_hash''', (cid,note_id,json_value(chunk['heading']),
                        chunk['start_line'],chunk['end_line'],chunk['text'],digest(title+'\0'+json_value(chunk['heading'])+'\0'+chunk['text'])))
                    db.execute('INSERT INTO chunks_fts VALUES(?,?,?,?,?,?)', (cid,title,' '.join(parsed['aliases']),
                        ' / '.join(chunk['heading']),chunk['text'],json_value(parsed['properties'])))
                if chunk_ids:
                    db.execute('DELETE FROM chunks WHERE note_id=? AND id NOT IN ('+','.join('?' for _ in chunk_ids)+')',(note_id,*chunk_ids))
                else:
                    db.execute('DELETE FROM chunks WHERE note_id=?',(note_id,))
                for target, kind, key, context, line in parsed['edges']:
                    db.execute('INSERT INTO edges(source_id,target,edge_type,property_key,context,line) VALUES(?,?,?,?,?,?)',
                        (note_id,target,kind,key,context,line))
                changed += 1
            for rel in (absent - used) | (set(errors) & set(old)):
                nid = old[rel]['id']
                db.execute('DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE note_id=?)', (nid,))
                db.execute('DELETE FROM notes WHERE id=?', (nid,))
                changed += 1
            self.resolve_edges(db)
            generation = int(self.meta(db, 'generation', '0')) + (1 if changed else 0)
            self.set_meta(db, 'generation', generation)
            self.set_meta(db, 'chunk_config', chunk_config)
            self.set_meta(db, 'last_scan', now())
            # Edits made while this scan was reading are caught next time.
            self.set_meta(db, 'scan_started', scan_started)
            self.set_meta(db, 'scan_errors', json_value(errors))
        return self.status() | {'changed':changed, 'discovered':len(paths)}

    def resolve_edges(self, db):
        notes = [dict(r) for r in db.execute('SELECT id,path,title,aliases FROM notes')]
        paths, names = {}, {}
        for note in notes:
            paths.setdefault(re.sub(r'\.md$', '', note['path'], flags=re.I).casefold(), []).append(note['id'])
            for name in set([Path(note['path']).stem, note['title'], *json.loads(note['aliases'])]):
                names.setdefault(name.casefold(), set()).add(note['id'])
        source_paths = {n['id']:n['path'] for n in notes}
        for edge in db.execute('SELECT * FROM edges').fetchall():
            target = re.sub(r'\.md$', '', edge['target'].split('#', 1)[0].strip(), flags=re.I)
            source = source_paths[edge['source_id']]
            nearby = posixpath.normpath(posixpath.join(posixpath.dirname(source), target)).casefold()
            qualified = posixpath.normpath(target.lstrip('/')).casefold()
            if not target:
                matches = {edge['source_id']}
            elif nearby.startswith('../'):
                matches = set()
            else:
                matches = set(paths.get(nearby, ())) or set(paths.get(qualified, ()))
                if not matches and '/' not in target:
                    matches = names.get(target.casefold(), set())
            state = 'resolved' if len(matches) == 1 else 'ambiguous' if matches else 'unresolved'
            db.execute('UPDATE edges SET target_id=?,resolution=? WHERE id=?',
                (next(iter(matches)) if len(matches) == 1 else None,state,edge['id']))

    def status(self):
        with self.connect() as db:
            return {'vault_id':self.vault_id, 'schema_version':SCHEMA, 'indexer_version':INDEXER,
                'generation':int(self.meta(db,'generation','0')), 'last_scan':self.meta(db,'last_scan') or None,
                'notes':db.execute('SELECT count(*) FROM notes').fetchone()[0],
                'chunks':db.execute('SELECT count(*) FROM chunks').fetchone()[0],
                'edges':db.execute('SELECT count(*) FROM edges').fetchone()[0],
                'unresolved_links':db.execute("SELECT count(*) FROM edges WHERE resolution!='resolved'").fetchone()[0],
                'notes_with_parse_errors':db.execute("SELECT count(*) FROM notes WHERE errors!='[]'").fetchone()[0],
                'parse_error_sample':[{'rel_path':r['path'],'errors':json.loads(r['errors'])}
                    for r in db.execute("SELECT path,errors FROM notes WHERE errors!='[]' ORDER BY path LIMIT 10")],
                'scan_errors':json.loads(self.meta(db,'scan_errors','{}')),
                'health':db.execute('PRAGMA quick_check').fetchone()[0],
                'embedding_status':'available' if db.execute('SELECT count(*) FROM embedding_spaces').fetchone()[0] else 'not_configured',
                'embedding_spaces':[dict(r) for r in db.execute('''SELECT s.*,count(c.id) AS embedded
                    FROM embedding_spaces s LEFT JOIN chunk_vectors v ON v.space_id=s.id
                    LEFT JOIN chunks c ON c.id=v.chunk_id AND v.chunk_hash=c.content_hash
                    GROUP BY s.id LIMIT 20''')]}

    def _filters(self, filters, prefix, *, alias='n'):
        prefix = relative(prefix)
        if not isinstance(filters, dict) or len(filters) > 12:
            raise ValueError('Use at most twelve structured property filters')
        clauses, params = [], []
        if prefix:
            clauses.append(f'({alias}.path=? OR substr({alias}.path,1,?)=?)')
            params.extend((prefix, len(prefix) + 1, prefix + '/'))
        for key, value in filters.items():
            if not isinstance(key,str) or not key or len(key)>120:
                raise ValueError('Invalid property key')
            base = f'SELECT 1 FROM properties p WHERE p.note_id={alias}.id AND p.key=?'
            if isinstance(value,dict):
                if set(value) == {'exists'} and type(value['exists']) is bool:
                    clauses.append(('' if value['exists'] else 'NOT ') + 'EXISTS('+base+')')
                    params.append(key)
                    continue
                if set(value) != {'in'} or not isinstance(value['in'],list) or not 1 <= len(value['in']) <= 20:
                    raise ValueError('Filters support equality, exists or in')
                values = value['in']
            else:
                values = [value]
            if any(not isinstance(v,(str,int,float,bool,type(None))) for v in values):
                raise ValueError('Property filter values must be scalar')
            clauses.append('EXISTS('+base+' AND p.value IN ('+','.join('?' for _ in values)+'))')
            params.extend([key, *map(json_value, values)])
        return (' AND '.join(clauses) or '1'), params

    def search(self, query='', *, mode='lexical', property_filters=None, path_prefix='', top_k=8, offset=0):
        if mode not in {'lexical','metadata_only'}:
            raise ValueError('Deterministic search supports lexical or metadata_only')
        if not isinstance(query,str) or len(query)>500 or type(top_k) is not int or not 1<=top_k<=50 or type(offset) is not int or not 0<=offset<=10000:
            raise ValueError('Invalid search query or page bounds')
        where, args = self._filters(property_filters or {}, path_prefix)
        match_strategy = 'exact_all_terms'
        with self.connect() as db:
            if mode == 'metadata_only':
                rows = db.execute(f'SELECT n.* FROM notes n WHERE {where} ORDER BY path LIMIT ? OFFSET ?', (*args,top_k+1,offset)).fetchall()
                matches = [{'note_id':r['id'],'rel_path':r['path'],'title':r['title'],'frontmatter':json.loads(r['frontmatter']),
                    'content_hash':r['content_hash']} for r in rows[:top_k]]
                for item in matches:
                    # Full properties stay indexed for exact filters; the
                    # model receives a bounded view with an explicit flag.
                    if len(json_value(item['frontmatter']))>4000:
                        item['frontmatter_excerpt']=json_value(item.pop('frontmatter'))[:4000]
                        item['frontmatter_truncated']=True
            else:
                tokens = re.findall(r'\w+',query, re.UNICODE)[:24]
                if not tokens:
                    return {'matches':[], 'next_offset':None, 'generation':int(self.meta(db,'generation','0'))}
                expression = ' AND '.join('"'+t+'"' for t in tokens)
                # Relax only when the complete conjunctive result set is empty,
                # not merely when a later page is empty. Filters remain identical.
                exists = db.execute(f"""SELECT 1 FROM chunks_fts
                    JOIN chunks c ON c.id=chunks_fts.chunk_id JOIN notes n ON n.id=c.note_id
                    WHERE chunks_fts MATCH ? AND {where} LIMIT 1""", (expression,*args)).fetchone()
                if not exists and len(tokens)>1:
                    expression = ' OR '.join('"'+t+'"' for t in dict.fromkeys(tokens))
                    match_strategy = 'relaxed_any_term'
                rows = db.execute(f"""SELECT c.*,n.path,n.title,n.content_hash AS note_hash,bm25(chunks_fts) AS rank
                    FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.chunk_id JOIN notes n ON n.id=c.note_id
                    WHERE chunks_fts MATCH ? AND {where} ORDER BY rank,n.path,c.start_line LIMIT ? OFFSET ?""",
                    (expression,*args,top_k+1,offset)).fetchall()
                matches = [{'chunk_id':r['id'],'note_id':r['note_id'],'rel_path':r['path'],'title':r['title'],
                    'start_line':r['start_line'],'end_line':r['end_line'],'excerpt':r['text'][:2000],
                    'excerpt_truncated':len(r['text'])>2000, 'heading':json.loads(r['heading']),
                    'rank':r['rank'],'content_hash':r['note_hash']} for r in rows[:top_k]]
            generation = int(self.meta(db,'generation','0'))
        unavailable = []
        accessible = []
        for item in matches:
            try:
                FilesystemResource.resolve(self.seal,str(self.root/item['rel_path'])).validate()
                accessible.append(item)
            except (OSError,ValueError,RuntimeError):
                unavailable.append(item['rel_path'])
        matches = accessible
        neighbors, seen = [], set()
        omitted = 0
        for match in matches:
            page = self.traverse(match['note_id'],limit=5)
            omitted += page['omitted']
            for edge in page['edges']:
                if edge['id'] not in seen:
                    seen.add(edge['id'])
                    if len(neighbors)<5:
                        neighbors.append(edge)
                    else:
                        omitted += 1
        return {'matches':matches,'graph_context':neighbors,'omitted_neighbors':omitted,
            'generation':generation,'next_offset':offset+top_k if len(rows)>top_k else None,
            'mode':mode, 'match_strategy':match_strategy if mode=='lexical' else 'metadata_filter', 'source':'local_markdown_index', 'unavailable_sources':unavailable,
            'last_scan':self.status()['last_scan']}

    def lookup(self, db, note_id):
        if not isinstance(note_id,str) or len(note_id)>512:
            raise ValueError('Invalid note selector')
        row = db.execute('SELECT * FROM notes WHERE id=? OR path=?', (note_id,note_id)).fetchone()
        if row is None:
            raise ValueError('Note is absent from this scoped index')
        return row

    def source_accessible(self,path):
        try:
            FilesystemResource.resolve(self.seal,str(self.root/relative(path,empty=False))).validate()
            return True
        except (OSError,ValueError,RuntimeError):
            return False

    def traverse(self, note_id, *, direction='both', edge_type=None, search_filter=None, limit=10, offset=0):
        if direction not in {'both','in','out'} or edge_type not in {None,'property','inline','embed'} or type(limit) is not int or not 1<=limit<=50 or type(offset) is not int or not 0<=offset<=10000:
            raise ValueError('Invalid graph traversal bounds or type')
        if search_filter is not None and (not isinstance(search_filter,str) or len(search_filter)>200):
            raise ValueError('Invalid graph filter')
        with self.connect() as db:
            note = self.lookup(db,note_id)
            if not self.source_accessible(note['path']):
                raise ValueError('Graph source is no longer accessible within this scope')
            clause = 'e.source_id=?' if direction=='out' else 'e.target_id=?' if direction=='in' else '(e.source_id=? OR e.target_id=?)'
            args = [note['id']] * (2 if direction=='both' else 1)
            if edge_type:
                clause += ' AND e.edge_type=?'
                args.append(edge_type)
            if search_filter:
                clause += ' AND (instr(lower(e.context),lower(?))>0 OR instr(lower(e.target),lower(?))>0 OR e.property_key=?)'
                args.extend([search_filter]*3)
            count = db.execute(f'SELECT count(*) FROM edges e WHERE {clause}',args).fetchone()[0]
            db.create_function('edge_weight',2,lambda kind,degree:self.edge_weights[kind]/math.log1p(max(1,degree)))
            rows = db.execute(f"""SELECT e.*,s.path AS source_path,t.path AS target_path,
                edge_weight(e.edge_type,(SELECT count(*) FROM edges incoming WHERE incoming.target_id=e.target_id)) AS weight
                FROM edges e JOIN notes s ON s.id=e.source_id LEFT JOIN notes t ON t.id=e.target_id
                WHERE {clause} ORDER BY weight DESC,s.path,e.line,e.id LIMIT ? OFFSET ?""",(*args,limit,offset)).fetchall()
            edges=[dict(r) for r in rows if self.source_accessible(r['source_path'])
                and (r['target_path'] is None or self.source_accessible(r['target_path']))]
            return {'note_id':note['id'],'edges':edges, 'omitted':max(0,count-offset-limit),
                'unavailable_edges':len(rows)-len(edges),
                'next_offset':offset+limit if count>offset+limit else None,
                'generation':int(self.meta(db,'generation','0'))}

    def read_range(self, note_id, start_line=1, end_line=40):
        if type(start_line) is not int or type(end_line) is not int or start_line<1 or end_line<start_line or end_line-start_line>=200:
            raise ValueError('Read 1–200 positive source lines')
        with self.connect() as db:
            note = dict(self.lookup(db,note_id))
            generation = int(self.meta(db,'generation','0'))
        text, _ = self.read(note['path'])
        lines = text.splitlines()
        selected = lines[start_line-1:end_line]
        raw = '\n'.join(selected)
        if len(raw)>16000:
            raise ValueError('Range exceeds 16,000-character budget; request fewer lines')
        return {'note_id':note['id'],'rel_path':note['path'],'start_line':start_line,
            'end_line':min(end_line,len(lines)), 'text':raw,
            'line_numbered':'\n'.join(f'{start_line+i}: {v}' for i,v in enumerate(selected)),
            'content_hash':digest(text),'indexed_content_hash':note['content_hash'],
            'stale':digest(text)!=note['content_hash'],'generation':generation}
