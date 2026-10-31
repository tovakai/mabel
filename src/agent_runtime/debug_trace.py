"""Bounded, private diagnostic sink for the existing chat event stream.

The causal JSONL ledger remains authoritative for its own events. This
owner/session-scoped SQLite sink indexes redacted runtime observations for UI
replay; it never records prompt text, tool output text or credentials.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from queue import Queue, Empty, Full
from threading import Lock, Thread
from uuid import uuid4
import json
import hashlib
import logging
import os
import re
import sqlite3
import time

logger = logging.getLogger(__name__)
_SAFE_NAME = re.compile(r'^[A-Za-z0-9_.:/-]{1,120}$')
_SECRET = re.compile(r'(?:bearer|password|secret|api[_-]?key|token|cookie|credential)', re.I)
_SECRET_VALUE = re.compile(r'^(?:sk[-_]|ghp_|xox[baprs]-|eyJ)', re.I)
_store = None
_store_lock = Lock()


def _name(value, fallback='unknown'):
    value = str(value or '')
    if value == 'first_token': return value
    return value if _SAFE_NAME.fullmatch(value) and not _SECRET.search(value) and not _SECRET_VALUE.search(value) else fallback


def _number(value):
    if type(value) in (int, float) and 0 <= value <= 10**12:
        return value
    return None


def _argument_shape(value):
    raw = value if isinstance(value, str) else ''
    try:
        parsed = json.loads(raw)
        keys = sorted(k for k in parsed if isinstance(k, str) and _SAFE_NAME.fullmatch(k)
                      and not _SECRET.search(k))[:32] if isinstance(parsed, dict) else []
    except (ValueError, TypeError):
        keys = []
    result={'argument_keys': keys, 'argument_bytes': len(raw.encode('utf-8'))}
    if isinstance(locals().get('parsed'),dict):
        if parsed.get('mode') in {'lexical','metadata_only','semantic','hybrid'}: result['retrieval_mode']=parsed['mode']
        if _number(parsed.get('top_k')) is not None: result['top_k']=parsed['top_k']
    return result


def _sanitize_metadata(value):
    if not isinstance(value,dict): return {}
    numeric={'round','prompt_tokens','schema_count','input_tokens','output_tokens',
             'request_context_tokens','context_length','argument_bytes','result_bytes',
             'exit_code','model_requests','tool_execution_ms','approval_wait_ms',
             'fallbacks','errors','dropped_events','model_execution_ms','result_count','top_k','index_revision',
             'prefill_ms','generation_ms','gen_tps','prefill_samples','generation_samples','retries','candidate_index','http_status',
             'tokens_before','tokens_after','messages_before','messages_after','memory_count',
             'unattributed_model_ms','first_token_ms','unattributed_first_token_ms','repeat_count','full_result_bytes','research_offered','batch_offered','input_growth_tokens',
             'raw_chars','visible_chars','reasoning_chars','tool_call_count','offered_count','executable_count',
             'authorized_count','denied_count','catalog_count','discovered_count','next_offset'}
    labels={'model','endpoint_id','failure_kind','usage_source','context_usage_source','stream','wait_duration','approval_wait','retrieval_mode','retry_reason','completion_status','approval_scope','approval_wait_source','role','timing_status','completion_basis','queue_timing','cache_timing','model_load_timing',
            'error_class','recovery_source','selection_mode','semantic_index'}
    result={}
    for key,item in value.items():
        if key in numeric:
            number=_number(item)
            if number is not None: result[key]=number
        elif key in labels:
            safe=_name(item,'')
            if safe: result[key]=safe
        elif key in {'argument_keys','offered_tools','selected_tools','requested_tools'} and isinstance(item,list):
            result[key]=[_name(x,'') for x in item[:32] if _name(x,'')]
    return result


class TraceStore:
    def __init__(self, path, *, max_events=100000, retention_days=14, queue_size=2048, ledger=None):
        self.path = Path(path)
        self.max_events = max(100, min(int(max_events), 1000000))
        self.retention_days = max(1, min(int(retention_days), 365))
        self.ledger = ledger
        self.queue = Queue(maxsize=max(1, int(queue_size)))
        self._drops = {}
        self._drop_lock = Lock()
        self._thread = Thread(target=self._run, name='mabel-debug-writer', daemon=True)
        self._thread.start()

    def emit(self, owner, session, turn, category, operation, status, *, severity='INFO', duration_ms=None, metadata=None, invocation=None, timestamp=None):
        if not all(isinstance(v, str) and v for v in (owner,session,turn)):
            return False
        if not isinstance(timestamp,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00',timestamp):
            timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        event = {'schema_version':1, 'timestamp':timestamp,
                 'owner':owner, 'session_id':session, 'turn_id':turn,
                 'invocation_id':('call_'+hashlib.sha256(str(invocation).encode()).hexdigest()[:16] if invocation else ''),
                 'severity':severity if severity in {'DEBUG','INFO','WARN','ERROR'} else 'INFO',
                 'category':category if category in {'MODEL','TOOL','RAG','RUNTIME','AUTH','MEMORY'} else 'RUNTIME',
                 'operation':_name(operation), 'status':_name(status),
                 'duration_ms':_number(duration_ms), 'metadata':_sanitize_metadata(metadata)}
        try:
            self.queue.put_nowait(event)
            return True
        except Full:
            with self._drop_lock:
                key=(owner,session,turn)
                if key in self._drops or len(self._drops)<256:
                    self._drops[key] = self._drops.get(key,0)+1
            return False

    def _connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        db=sqlite3.connect(str(self.path),timeout=10)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS trace_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL, timestamp TEXT NOT NULL, payload TEXT NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS trace_session ON trace_events(owner,session_id,seq)')
        db.commit()
        try: os.chmod(self.path,0o600)
        except OSError: pass
        return db

    def _write(self, db, event):
        db.execute('INSERT INTO trace_events(owner,session_id,turn_id,timestamp,payload) VALUES(?,?,?,?,?)',
                   (event['owner'],event['session_id'],event['turn_id'],event['timestamp'],json.dumps(event,ensure_ascii=False,allow_nan=False)))

    def _run(self):
        db=None; writes=0
        while True:
            try:
                event=self.queue.get(timeout=1)
            except Empty:
                with self._drop_lock:
                    pending,self._drops=self._drops,{}
                if pending:
                    try:
                        if db is None: db=self._connect()
                        for (owner,session,turn),count in pending.items():
                            marker={'schema_version':1,'timestamp':datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                                    'owner':owner,'session_id':session,'turn_id':turn,'invocation_id':'',
                                    'severity':'WARN','category':'RUNTIME','operation':'buffer_overflow',
                                    'status':'dropped','duration_ms':None,'metadata':{'dropped_events':count}}
                            self._write(db,marker)
                        db.commit()
                    except Exception:
                        logger.warning('Debug trace drop marker failed',exc_info=True)
                        with self._drop_lock:
                            for key,count in pending.items(): self._drops[key]=self._drops.get(key,0)+count
                        if db is not None: db.close(); db=None
                    finally:
                        if db is not None: db.close(); db=None
                continue
            if event is None:
                self.queue.task_done(); break
            dropped=0
            try:
                if db is None: db=self._connect()
                with self._drop_lock:
                    dropped=self._drops.pop((event['owner'],event['session_id'],event['turn_id']),0)
                if dropped:
                    marker=dict(event,category='RUNTIME',operation='buffer_overflow',status='dropped',
                                severity='WARN',duration_ms=None,metadata={'dropped_events':dropped})
                    self._write(db,marker)
                self._write(db,event); writes+=1
                if writes % 100 == 0:
                    cutoff=datetime.fromtimestamp(time.time()-self.retention_days*86400,timezone.utc).isoformat(timespec='milliseconds')
                    db.execute('DELETE FROM trace_events WHERE timestamp < ?', (cutoff,))
                    db.execute('DELETE FROM trace_events WHERE seq <= (SELECT COALESCE(MAX(seq),0)-? FROM trace_events)', (self.max_events,))
                db.commit()
                if self.ledger is not None:
                    try:
                        from src.agent_runtime.event_ledger import Event
                        kind = ('runtime.agent_turn' if event['operation']=='turn' else
                                'runtime.tool_discovery' if event['operation']=='discover_tools' else None)
                        if kind:
                            self.ledger.append(Event.create('runtime',kind,correlation_id=event['turn_id'],
                                session_id=event['session_id'],decision=event['status']))
                    except Exception:
                        logger.warning('Causal event ledger append failed',exc_info=True)
            except Exception:
                logger.warning('Debug trace persistence failed',exc_info=True)
                with self._drop_lock:
                    key=(event['owner'],event['session_id'],event['turn_id'])
                    if key in self._drops or len(self._drops)<256:
                        self._drops[key]=self._drops.get(key,0)+dropped+1
                if db is not None:
                    try: db.close()
                    except Exception: pass
                    db=None
            finally:
                if db is not None:
                    try: db.close()
                    except Exception: logger.warning('Debug database close failed',exc_info=True)
                    db=None
                self.queue.task_done()
        if db is not None: db.close()

    def read(self, owner, session, *, after=0, limit=500, turn=None):
        limit=max(1,min(int(limit),1000)); after=max(0,int(after))
        if not self.path.exists(): return []
        db=self._connect()
        try:
            sql='SELECT seq,payload FROM trace_events WHERE owner=? AND session_id=? AND seq>?'
            args=[owner,session,after]
            if turn: sql+=' AND turn_id=?'; args.append(turn)
            rows=db.execute(sql+' ORDER BY seq LIMIT ?', (*args,limit)).fetchall()
            return [dict(json.loads(row['payload']),seq=row['seq']) for row in rows]
        finally: db.close()

    def close(self):
        self.queue.put(None)
        self._thread.join(timeout=10)


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            from src.constants import DATA_DIR
            from src.agent_runtime.event_ledger import Ledger
            def setting(name,default):
                try: return int(os.environ.get(name,default))
                except (TypeError,ValueError): return default
            _store=TraceStore(Path(DATA_DIR)/'debug_console.sqlite3',
                              max_events=setting('MABEL_DEBUG_MAX_EVENTS',100000),
                              retention_days=setting('MABEL_DEBUG_RETENTION_DAYS',14),
                              ledger=Ledger(Path(DATA_DIR)/'phase0'/'events.jsonl'))
        return _store


class TraceObserver:
    def __init__(self, store, owner, session, *, started=None, started_wall=None):
        self.store,self.owner,self.session=store,str(owner),str(session)
        self.turn='turn_'+uuid4().hex
        self.started=started if started is not None else time.monotonic()
        self.tools={}; self.model_started=None; self.first_token=False; self.first_token_ms=None; self.seen_calls={}
        self.visible_chars=0; self.reasoning_chars=0; self.stream_tool_calls=0
        self.tool_ms=0; self.model_ms=0; self.model_requests=0; self.errors=0; self.fallbacks=0; self.retries=0; self.stream_error=False
        self.prefill_ms=0; self.generation_ms=0; self.prefill_samples=0; self.generation_samples=0
        self.approval_ms=None; self._approval_recorded=False
        self.usage={}
        self.emit('RUNTIME','request','received',timestamp=started_wall)
        self.emit('RUNTIME','turn','started',timestamp=started_wall)

    def emit(self, category, operation, status, **kwargs):
        self.store.emit(self.owner,self.session,self.turn,category,operation,status,**kwargs)

    def observe(self, chunk):
        if not isinstance(chunk,str): return
        if len(chunk)>262144:
            self.emit('RUNTIME','oversized_stream_event','omitted',severity='WARN',metadata={'result_bytes':len(chunk.encode('utf-8'))})
            return
        for line in chunk.splitlines():
            if line.startswith('event: error'):
                self.errors+=1; self.stream_error=True; self.emit('RUNTIME','stream','error',severity='ERROR')
                continue
            if not line.startswith('data: ') or line[6:]=='[DONE]': continue
            try: data=json.loads(line[6:])
            except (ValueError,TypeError): continue
            if not isinstance(data,dict): continue
            kind=data.get('type'); now=time.monotonic()
            if kind=='diagnostic_event':
                operation=_name(data.get('operation')); status=_name(data.get('status'))
                if operation=='model_request' and status=='started':
                    self.model_started=now; self.first_token=False; self.first_token_ms=None; self.model_requests+=1
                    self.visible_chars=0; self.reasoning_chars=0; self.stream_tool_calls=0
                if operation=='model_retry': self.retries+=1
                duration=(now-self.model_started)*1000 if self.model_started and (
                    (operation=='model_request' and status in {'completed','error','empty'}) or operation=='first_token') else None
                if operation=='grace_synthesis':
                    if status=='started':
                        self.model_requests+=1
                    if _number(data.get('duration_ms')) is not None:
                        duration=data['duration_ms']
                        if status in {'completed','failed'}: self.model_ms+=duration
                    if status=='failed': self.errors+=1
                if operation=='first_token': self.first_token=True; self.first_token_ms=duration
                if operation=='model_request' and status in {'completed','error','empty'}:
                    if duration is not None: self.model_ms+=duration
                    if status=='error': self.errors+=1
                    if _number(data.get('prefill_ms')) is not None:
                        self.prefill_ms+=data['prefill_ms']; self.prefill_samples+=1
                    if _number(data.get('generation_ms')) is not None:
                        self.generation_ms+=data['generation_ms']; self.generation_samples+=1
                    self.model_started=None
                metadata={k:_number(data.get(k)) for k in (
                    'round','prompt_tokens','schema_count','input_tokens','output_tokens',
                    'prefill_ms','generation_ms','gen_tps','candidate_index',
                    'research_offered','batch_offered','raw_chars','visible_chars',
                ) if _number(data.get(k)) is not None}
                if operation=='model_request' and status in {'completed','error','empty'}:
                    metadata['visible_chars']=self.visible_chars
                    metadata['reasoning_chars']=self.reasoning_chars
                    metadata['tool_call_count']=self.stream_tool_calls
                for field in ('failure_kind','error_class','recovery_source'):
                    if data.get(field): metadata[field]=_name(data[field])
                if operation=='model_request' and status=='completed' and duration is not None:
                    prefill=_number(data.get('prefill_ms')); generation=_number(data.get('generation_ms'))
                    if self.first_token_ms is not None: metadata['first_token_ms']=round(self.first_token_ms,3)
                    if prefill is not None and self.first_token_ms is not None and self.first_token_ms>=prefill:
                        metadata['unattributed_first_token_ms']=round(self.first_token_ms-prefill,3)
                    if prefill is not None and generation is not None and duration>=prefill+generation:
                        metadata['unattributed_model_ms']=round(duration-prefill-generation,3)
                        metadata['timing_status']='provider_fields_not_wall_phases'
                    # Provider queue, cache hits, and model-load time are not in this trace.
                    for phase in ('queue_timing','cache_timing','model_load_timing'):
                        metadata[phase]=_name(data.get(phase), 'unavailable') if data.get(phase) else 'unavailable'
                    current=_number(data.get('input_tokens'))
                    previous=getattr(self, 'previous_input_tokens', None)
                    if current is not None and previous is not None and current>=previous:
                        metadata['input_growth_tokens']=current-previous
                    if current is not None:
                        self.previous_input_tokens=current
                if data.get('model'): metadata['model']=_name(data['model'])
                if data.get('endpoint_id'): metadata['endpoint_id']=_name(data['endpoint_id'])
                if data.get('stream'): metadata['stream']=_name(data['stream'])
                if data.get('retry_reason'): metadata['retry_reason']=_name(data['retry_reason'])
                if operation=='model_request': metadata['usage_source']='estimated' if status=='started' else ('provider' if data.get('input_tokens') is not None else 'unavailable')
                self.emit('MODEL',operation,status,
                          severity='ERROR' if status in {'error','failed'} else 'INFO',
                          duration_ms=duration,metadata=metadata)
            elif kind=='turn_contract':
                offered=data.get('offered')
                executable=data.get('executable')
                permitted=data.get('authorized_tools')
                denied=data.get('denied_tools')
                self.emit('RUNTIME','tool_catalog','offered',metadata={
                    'selection_mode':data.get('selection_mode'),
                    'offered_count':len(offered) if isinstance(offered,list) else None,
                    'executable_count':len(executable) if isinstance(executable,list) else None,
                    'authorized_count':len(permitted) if isinstance(permitted,list) else None,
                    'denied_count':len(denied) if isinstance(denied,list) else None,
                    'offered_tools':offered if isinstance(offered,list) else [],
                })
            elif kind=='tool_start':
                tool=_name(data.get('tool')); call=_name(data.get('call_id') or data.get('tool_call_id'),tool)
                self.tools[call]=now
                category='RAG' if tool.startswith('vault_') or tool=='dispatch_dottie' else 'TOOL'
                command=data.get('full_command') or data.get('command') or ''
                shape=_argument_shape(command)
                # Keep only a per-turn, in-memory fingerprint; never persist paths or arguments.
                if tool in {'read_file','vault_read_range','vault_knowledge_search','vault_traverse_graph','get_workspace'} and isinstance(command,str) and command.strip():
                    fingerprint=hashlib.sha256((tool+'\0'+command).encode()).digest()
                    count=self.seen_calls.get(fingerprint,0)
                    if count: shape['repeat_count']=count
                    if len(self.seen_calls)<1024 or fingerprint in self.seen_calls: self.seen_calls[fingerprint]=count+1
                self.emit(category,tool,'started',invocation=call,metadata=shape)
            elif kind=='tool_output':
                tool=_name(data.get('tool')); call=_name(data.get('call_id') or data.get('tool_call_id'),tool)
                started=self.tools.pop(call,None); duration=(now-started)*1000 if started is not None else None
                if duration is not None: self.tool_ms+=duration
                failed=bool(data.get('error') or data.get('blocked') or data.get('exit_code') not in (None,0))
                if failed: self.errors+=1
                metadata={'result_bytes':len(str(data.get('output') or '').encode('utf-8'))}
                if _number(data.get('full_result_bytes')) is not None: metadata['full_result_bytes']=data['full_result_bytes']
                if _number(data.get('exit_code')) is not None: metadata['exit_code']=data['exit_code']
                if data.get('failure_kind'): metadata['failure_kind']=_name(data['failure_kind'])
                if tool=='discover_tools':
                    raw_discovery=data.get('output')
                    if isinstance(raw_discovery,str) and len(raw_discovery)<=65536:
                        try:
                            response=json.loads(raw_discovery)
                            if isinstance(response,dict):
                                selected=response.get('selected')
                                if isinstance(selected,list): metadata['selected_tools']=selected[:32]
                                if response.get('retrieval'): metadata['retrieval_mode']=response['retrieval']
                                if response.get('semantic_index'): metadata['semantic_index']=response['semantic_index']
                                if isinstance(response.get('tools'),list):
                                    metadata['catalog_count']=len(response['tools'])
                        except (ValueError,TypeError):
                            pass
                    requested=data.get('requested_names')
                    if isinstance(requested,list): metadata['requested_tools']=requested[:32]
                raw=data.get('output')
                if tool.startswith('vault_') and isinstance(raw,str) and len(raw)<=65536:
                    try:
                        result=json.loads(raw)
                        if isinstance(result,dict):
                            for key in ('matches','results','findings'):
                                if isinstance(result.get(key),list): metadata['result_count']=len(result[key]); break
                            if _number(result.get('index_revision')) is not None: metadata['index_revision']=result['index_revision']
                    except (ValueError,TypeError): pass
                if tool=='dispatch_dottie' and isinstance(raw,str) and len(raw)<=65536:
                    try:
                        routing=json.loads(raw).get('model_routing')
                        if isinstance(routing,dict):
                            self.emit('MODEL','research_role','completed',metadata={'role':'research','model':routing.get('model'),'endpoint_id':routing.get('endpoint_id')})
                    except (ValueError,TypeError,AttributeError): pass
                category='RAG' if tool.startswith('vault_') or tool=='dispatch_dottie' else 'TOOL'
                self.emit(category,tool,'error' if failed else 'completed',severity='ERROR' if failed else 'INFO',duration_ms=duration,invocation=call,metadata=metadata)
                if data.get('ask_user'):
                    card=data['ask_user'] if isinstance(data['ask_user'],dict) else {}
                    self.emit('AUTH','approval','requested',invocation=card.get('approval_id'),metadata={'wait_duration':'unavailable'})
            elif kind=='ask_user':
                card=data.get('data') if isinstance(data.get('data'),dict) else {}
                if card.get('approval_id'):
                    self.emit('AUTH','approval','requested',invocation=card.get('approval_id'),metadata={'wait_duration':'unavailable'})
            elif kind=='tool_approval_resolved':
                if not self._approval_recorded:
                    self.emit('AUTH','approval',_name(data.get('decision'),'resolved'))
            elif kind=='agent_step':
                self.emit('RUNTIME','agent_iteration','started',metadata={'round':_number(data.get('round'))})
            elif kind in {'compacted','context_trimmed'}:
                info=data.get('data') if isinstance(data.get('data'),dict) else data
                self.emit('MEMORY','context_compaction','completed',metadata={key:info.get(key) for key in ('tokens_before','tokens_after','messages_before','messages_after')})
            elif kind=='memories_used':
                items=data.get('data')
                self.emit('MEMORY','memory_retrieval','completed',metadata={'memory_count':len(items) if isinstance(items,list) else 0})
            elif kind=='completion_decision':
                info=data.get('data') if isinstance(data.get('data'),dict) else {}
                status='complete' if info.get('can_complete') is True else 'incomplete'
                metadata={'completion_status':_name(info.get('status'),'unknown')}
                if info.get('reason')=='no declared artifact or executable verifier was available': metadata['completion_basis']='no_declared_executable_verifier'
                self.emit('RUNTIME','completion_validation',status,metadata=metadata)
            elif kind in {'loop_breaker_triggered','intent_nudge_exhausted'}:
                self.emit('RUNTIME','replanning','bounded',severity='WARN')
            elif kind=='fallback':
                self.fallbacks+=1
                metadata={'model':_name(data.get('answered_by')),'endpoint_id':_name(data.get('answered_by_endpoint_id'),''),
                          'failure_kind':'provider_failure'}
                failures=data.get('failures') or []
                if isinstance(failures,list) and failures and isinstance(failures[0],dict):
                    metadata['http_status']=_number(failures[0].get('status'))
                self.emit('MODEL','fallback','activated',severity='WARN',metadata=metadata)
            elif kind=='metrics' and isinstance(data.get('data'),dict):
                metric=data['data']; self.usage={k:metric.get(k) for k in ('input_tokens','output_tokens','request_context_tokens','context_length','usage_source','context_usage_source') if k in metric}
            elif kind=='tool_calls':
                calls=data.get('calls')
                if isinstance(calls,list):
                    count=min(len(calls),64)
                    if self.model_started is not None:
                        self.stream_tool_calls+=count
                    elif count:
                        # Tool-call events may be emitted after the provider's
                        # model_request completed marker. Report their presence
                        # separately instead of silently implying no tool calls.
                        self.emit('MODEL','tool_calls','after_model_completion',
                                  metadata={'tool_call_count':count})
            elif 'delta' in data:
                delta=data.get('delta')
                if isinstance(delta,str) and self.model_started is not None:
                    if data.get('thinking') is True:
                        self.reasoning_chars+=len(delta)
                    else:
                        self.visible_chars+=len(delta)
                if not self.first_token:
                    self.first_token=True
                    duration=(now-self.model_started)*1000 if self.model_started else None
                    self.emit('MODEL','first_token','received',duration_ms=duration,
                              metadata={'stream':'reasoning' if data.get('thinking') else 'final'})

    def finish(self, status):
        if status=='completed' and self.stream_error: status='error'
        if self.model_started is not None:
            duration=(time.monotonic()-self.model_started)*1000
            self.model_ms+=duration
            self.emit('MODEL','model_request','interrupted',severity='WARN',duration_ms=duration)
            self.model_started=None
        self.emit('RUNTIME','turn',status,duration_ms=(time.monotonic()-self.started)*1000,
                  severity='ERROR' if status=='error' else 'INFO',metadata={
                      'model_requests':self.model_requests,'model_execution_ms':round(self.model_ms,3),
                      'tool_execution_ms':round(self.tool_ms,3),
                      'approval_wait_ms':self.approval_ms,'approval_wait_source':'wall_clock' if self.approval_ms is not None else 'unavailable',
                      'fallbacks':self.fallbacks,'errors':self.errors,'retries':self.retries,
                      'prefill_ms':round(self.prefill_ms,3) if self.prefill_samples else None,
                      'generation_ms':round(self.generation_ms,3) if self.generation_samples else None,
                      'prefill_samples':self.prefill_samples,'generation_samples':self.generation_samples,**self.usage})

    def approval_resolved(self, decision, approval_id, created_at, received_at):
        self._approval_recorded=True
        if _number(created_at) is None or _number(received_at) is None or received_at < created_at:
            duration=None
        else:
            duration=round((received_at-created_at)*1000,3)
            self.approval_ms=duration
        self.emit('AUTH','approval',_name(decision,'resolved'),duration_ms=duration,invocation=approval_id,
                  metadata={'approval_scope':_name(decision,'unknown'),'approval_wait_source':'wall_clock' if duration is not None else 'unavailable'})
