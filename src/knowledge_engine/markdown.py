"""Bounded YAML-node parsing and source-line-preserving Markdown extraction."""
from __future__ import annotations
import hashlib
import json
import re
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

LINK = re.compile(r'(!?)\[\[([^\]\n]+)\]\]')
HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*#*\s*$')
RELATIONS = frozenset({'parent', 'parents', 'related', 'project', 'projects', 'type',
    'category', 'categories', 'author', 'authors', 'owner', 'owned_by', 'depends_on'})


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()



# Split overlong *source lines*, not just whole paragraphs. Imported chat
# transcripts can contain 30+ KiB on a single line, which the ordinary
# paragraph budget cannot constrain. Keep pieces on their original line
# (the public evidence API uses line ranges) and avoid discarding characters.
# 1,600 characters is deliberately more conservative than the 3,200-character
# default chunk budget because embedding tokenizers need not use 4 chars/token.
LONG_LINE_LIMIT = 1600


def bounded_source_chunks(raw, start_line, maximum):
    """Return (text, start_line, end_line) with no chunk over `maximum`.

    Normally emits exactly the original chunk. Only oversize chunks are
    split, preserving existing stable chunk IDs and embeddings elsewhere.
    """
    cap = min(maximum, LONG_LINE_LIMIT)
    if len(raw) <= cap:
        return [(raw, start_line, start_line + raw.count('\n'))]
    chunks = []
    buffer = []
    buffer_chars = 0
    buffer_start = start_line

    def flush():
        nonlocal buffer, buffer_chars
        if buffer:
            joined = '\n'.join(buffer)
            if joined.strip():
                chunks.append((joined, buffer_start, buffer_start + len(buffer) - 1))
            buffer = []
            buffer_chars = 0

    for offset, line in enumerate(raw.split('\n')):
        line_number = start_line + offset
        if len(line) > cap:
            flush()
            remainder = line
            while remainder:
                if len(remainder) <= cap:
                    cut = len(remainder)
                else:
                    # Prefer whitespace boundaries while keeping every
                    # character, including separators, in source order.
                    boundary = max(remainder.rfind(' ', 0, cap + 1),
                                   remainder.rfind('\t', 0, cap + 1))
                    cut = boundary + 1 if boundary >= cap // 2 else cap
                part, remainder = remainder[:cut], remainder[cut:]
                if part.strip():
                    chunks.append((part, line_number, line_number))
            continue
        additional = len(line) if not buffer else len(line) + 1
        if buffer and buffer_chars + additional > cap:
            flush()
        if not buffer:
            buffer_start = line_number
            buffer_chars = len(line)
        else:
            buffer_chars += len(line) + 1
        buffer.append(line)
    flush()
    return chunks

def parse(text, *, relationship_keys=RELATIONS, maximum=3200, minimum=600):
    lines = text.splitlines()
    properties, edges, errors = {}, [], []
    body = 0
    if lines and lines[0].lstrip('\ufeff').strip() == '---':
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in {'---', '...'}), None)
        if end is None:
            errors.append('Unterminated frontmatter')
        else:
            body = end + 1
            source = '\n'.join(lines[1:end])
            try:
                if len(source) > 65536:
                    raise ValueError('Frontmatter exceeds 64 KiB')
                node = yaml.compose(source, Loader=yaml.SafeLoader)
                visits = [0]
                def convert(n, seen=frozenset()):
                    visits[0] += 1
                    if visits[0] > 4096 or id(n) in seen or len(seen) > 16:
                        raise ValueError('Frontmatter nesting/alias budget exceeded')
                    seen = seen | {id(n)}
                    if isinstance(n, ScalarNode):
                        if n.tag.endswith(':bool') and n.value.lower() in {'true', 'false'}:
                            return n.value.lower() == 'true'
                        if n.tag.endswith(':null'):
                            return None
                        if n.tag.endswith(':int') and re.fullmatch(r'[-+]?\d+', n.value):
                            return int(n.value)
                        if n.tag.endswith(':float'):
                            try:
                                number = float(n.value)
                                if number == number and abs(number) != float('inf'):
                                    return number
                            except ValueError:
                                pass
                        return n.value
                    if isinstance(n, SequenceNode):
                        return [convert(v, seen) for v in n.value]
                    if isinstance(n, MappingNode):
                        out = {}
                        for key, value in n.value:
                            if not isinstance(key, ScalarNode) or key.value in out:
                                raise ValueError('Duplicate/non-scalar YAML property')
                            out[key.value] = convert(value, seen)
                        return out
                    raise ValueError('Unsupported YAML node')
                if node is not None and not isinstance(node, MappingNode):
                    raise ValueError('Frontmatter must be a mapping')
                properties = convert(node) if node is not None else {}
                for key, value in node.value if node is not None else ():
                    if key.value in {'aliases', 'alias', 'tags'}:
                        continue
                    def references(n):
                        if isinstance(n, ScalarNode):
                            targets = list(LINK.finditer(n.value))
                            if targets:
                                for match in targets:
                                    edges.append((match[2].split('|', 1)[0], 'property', key.value,
                                                  n.value[:300], n.start_mark.line + 2))
                            elif key.value in relationship_keys and n.value.strip():
                                edges.append((n.value.strip(), 'property', key.value, n.value[:300],
                                              n.start_mark.line + 2))
                        elif isinstance(n, SequenceNode):
                            for v in n.value:
                                references(v)
                    references(value)
            except (ValueError, yaml.YAMLError, RecursionError):
                errors.append('Invalid or over-budget YAML frontmatter')
                properties, edges = {}, []
    aliases = properties.get('aliases', properties.get('alias', []))
    aliases = [aliases] if isinstance(aliases, str) else aliases if isinstance(aliases, list) else []
    aliases = [v for v in aliases if isinstance(v, str)]
    sections, heading, start, fenced, fence_char = [], [], body, False, ''
    for i in range(body, len(lines)):
        line = lines[i]
        fence = re.match(r'^\s{0,3}([' + chr(96) + r']{3,}|~{3,})', line)
        if fence:
            if not fenced:
                fenced, fence_char = True, fence[1][0]
            elif fence[1][0] == fence_char:
                fenced = False
            continue
        if fenced:
            continue
        match = HEADING.match(line)
        if match:
            if i > start:
                sections.append((start, i, list(heading)))
            level = len(match[1])
            heading = heading[:level - 1] + [match[2]]
            start = i
        inline = re.sub(chr(96) + '[^' + chr(96) + ']*' + chr(96), '', line)
        for match in LINK.finditer(inline):
            edges.append((match[2].split('|', 1)[0], 'embed' if match[1] else 'inline', '',
                          line[:300], i + 1))
    if start < len(lines):
        sections.append((start, len(lines), list(heading)))
    # Merge short adjacent sibling sections; their actual headings stay in
    # source text and the shared parent remains the context hierarchy.
    merged = []
    for section in sections:
        if merged:
            begin, end, hierarchy = merged[-1]
            if (hierarchy[:-1] == section[2][:-1]
                    and len("\n".join(lines[begin:end])) < minimum
                    and len("\n".join(lines[begin:section[1]])) <= maximum):
                merged[-1] = (begin, section[1], hierarchy[:-1])
                continue
        merged.append(section)
    chunks = []
    for begin, end, hierarchy in merged:
        pos = begin
        while pos < end:
            stop, size, paragraph = pos, 0, None
            while stop < end:
                additional = len(lines[stop]) + 1
                if stop > pos and size + additional > maximum:
                    break
                size += additional
                stop += 1
                if not lines[stop - 1].strip() and size >= minimum:
                    paragraph = stop
            if stop < end and paragraph is not None and paragraph > pos:
                stop = paragraph
            raw = '\n'.join(lines[pos:stop])
            if raw.strip():
                for part, start_line, end_line in bounded_source_chunks(raw, pos + 1, maximum):
                    chunks.append({'heading':hierarchy, 'start_line':start_line, 'end_line':end_line,
                                   'text':part, 'content_hash':digest(part)})
            pos = stop
    if not chunks and lines:
        raw = '\n'.join(lines)
        chunks = [{'heading':[], 'start_line':start_line, 'end_line':end_line,
                   'text':part, 'content_hash':digest(part)}
                  for part, start_line, end_line in bounded_source_chunks(raw, 1, maximum)]
    return {'properties':properties, 'aliases':aliases, 'chunks':chunks, 'edges':edges,
            'errors':errors, 'title':properties.get('title') if isinstance(properties.get('title'), str) else None}


def json_value(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)
