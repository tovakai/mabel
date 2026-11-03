"""Bounded, source-attributed recovery from already authorized file reads."""
from __future__ import annotations
import json


def excerpts(events, *, max_sources=4, max_chars=6000):
    """Use only successful current-turn reads; never fetch or infer new evidence."""
    selected=[]; seen=set(); remaining=max_chars
    expanded=[]
    for event in events:
        if not isinstance(event,dict) or event.get('exit_code') != 0 or event.get('blocked'):
            continue
        if event.get('tool')=='vault_retrieve_batch':
            try: batch=json.loads(event.get('output') or '')
            except (ValueError,TypeError): continue
            if isinstance(batch,dict):
                expanded.extend(item for item in batch.get('results',[]) if isinstance(item,dict))
        else: expanded.append(event)
    for event in reversed(expanded):
        if event.get('exit_code') != 0 or event.get('blocked'):
            continue
        tool=event.get('tool'); text=event.get('output')
        if tool=='vault_read_range':
            try: value=json.loads(text or '')
            except (ValueError,TypeError): continue
            if not isinstance(value,dict): continue
            path=value.get('rel_path'); text=value.get('text'); offset=value.get('start_line')
        elif tool=='read_file':
            command=event.get('command') or ''
            try: args=json.loads(command)
            except (TypeError,ValueError): args={'path':command}
            if not isinstance(args,dict): continue
            path=args.get('path'); offset=args.get('offset',0)
        else: continue
        if not isinstance(path,str): continue
        path=path.strip()
        if not path or '\n' in path or len(path)>512 or path in seen: continue
        if not isinstance(text,str) or not text.strip(): continue
        lines=text.splitlines()
        if type(offset) is not int or offset<0: continue
        start=max(offset,1); picked=[]; used=0
        in_frontmatter=bool(lines and lines[0].strip()=='---')
        for idx,line in enumerate(lines):
            if in_frontmatter:
                if idx and line.strip()=='---': in_frontmatter=False
                continue
            line=line.strip()
            if not line or line.startswith('... (truncated') or line.startswith('... [truncated'): continue
            if used+len(line)>min(1500,remaining): break
            picked.append((start+idx,line));used+=len(line)+16
            if len(picked)>=8: break
        if picked:
            seen.add(path); selected.append({'path':path,'lines':picked}); remaining-=used
        if len(selected)>=max_sources or remaining<100: break
    return list(reversed(selected))


def evidence_text(events):
    out=[]
    for item in excerpts(events):
        path=item['path'].replace('`',"'")
        lines='\n'.join(f'{n}: {text}' for n,text in item['lines'])
        out.append(f'Source: {path}\n{lines}')
    return '\n\n'.join(out)


def fallback(events):
    sources=excerpts(events)
    if not sources:return ''
    out=['I found source material, but could not produce a reliable summary. Here are excerpts from the files I read:']
    for item in sources:
        path=item['path'].replace('`',"'")
        out.append(f"\n**{path}**")
        out.extend(f'> Line {number}: {line}' for number,line in item['lines'])
    out.append('\nThese are excerpts, not a complete summary.')
    return '\n'.join(out)


def sanitize_vault_note_links(answer, events):
    """Remove invented note anchors when a vault source ID cannot verify the label.

    Native index IDs are not application-note anchors, even when the model
    reproduces one correctly; render a path instead of an invalid link.
    """
    import re
    sources={}
    saw_vault=False
    def add(value):
        if not isinstance(value,dict): return
        note=value.get('note_id'); path=value.get('rel_path')
        if isinstance(note,str) and isinstance(path,str):
            sources.setdefault(note,set()).add(path)
        for key in ('matches','results'):
            items=value.get(key)
            if isinstance(items,list):
                for item in items:
                    if not isinstance(item,dict):continue
                    add(item)
                    if isinstance(item.get('output'),str):
                        try:add(json.loads(item['output']))
                        except (ValueError,TypeError):pass
    for event in events:
        if not isinstance(event,dict) or event.get('exit_code') != 0 or event.get('blocked'):continue
        if event.get('tool') not in {'vault_knowledge_search','vault_read_range','vault_retrieve_batch'}:continue
        saw_vault=True
        try:add(json.loads(event.get('output') or ''))
        except (ValueError,TypeError):pass
    if not saw_vault:return answer
    def replace(match):
        label,identifier=match.group(1),match.group(2)
        paths=sources.get(identifier,set())
        matching=[p for p in paths if p.rsplit('/',1)[-1].casefold()==label.casefold()]
        if len(paths)==1 and len(matching)==1:
            return '`'+matching[0].replace('`',"'")+'`'
        return '`'+label.replace('`',"'")+'` (source ID unverified)'
    return re.sub(r'\[([^\]\n]{1,512})\]\(#note-([A-Za-z0-9_-]{1,128})\)',replace,answer)
