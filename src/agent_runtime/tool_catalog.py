"""Request-local schema discovery. Selection never changes execution grants."""
from contextvars import ContextVar
from contextlib import contextmanager
import json
import logging
import re

_ACTIVE = ContextVar('request_tool_catalog', default=None)
logger = logging.getLogger(__name__)

# Discovery-only compatibility names. These select a registered schema; they
# never execute a tool or grant authority. Only apply if the canonical schema
# survived the request's policy-filtered catalog.
_DISCOVERY_ALIASES = {"vault_search": "vault_knowledge_search"}


class DiscoveryError(ValueError):
    """Distinct discovery failure. error_code is not a generic collapse."""

    def __init__(self, code, message, *, recoverable=True, requested=()):
        super().__init__(message)
        self.code = code
        self.recoverable = bool(recoverable)
        self.requested = tuple(requested)


def _discovery_payload(error):
    message = str(error)
    requested = [name for name in getattr(error, "requested", ()) if isinstance(name, str)][:6]
    code = getattr(error, "code", "internal_discovery_failure")
    recoverable = bool(getattr(error, "recoverable", False))
    # Tool names and the code only. Arguments and secrets stay out of the log.
    logger.info(
        "discover_tools outcome code=%s recoverable=%s requested=%s catalog=%s",
        code, recoverable, requested,
        len(getattr(getattr(error, "catalog", None), "inventory", {}) or {}),
    )
    return {
        "status": "error",
        "tool": "discover_tools",
        "error": message,
        "message": message,
        "error_code": code,
        "failure_kind": code,
        "recoverable": recoverable,
        "executed": False,
        "exit_code": 1,
        "requested_names": requested,
    }




class ToolCatalog:
    def __init__(self, contract):
        self.contract = contract
        self.contracts = [contract]
        # catalog_json is the server's policy-filtered discovery inventory.
        # A compact runtime's initial executable subset may intentionally be
        # smaller; discovery must not erase vetted alternatives with it.
        self.full_inventory = {s['function']['name']:s for s in map(json.loads, contract.catalog_json)}
        self.inventory = dict(self.full_inventory)
        from .authority import active_request_authority
        from src.turn_contract import canonical_tool
        authority = active_request_authority()
        if authority is not None:
            self.inventory = {name:schema for name,schema in self.inventory.items()
                if not authority.block_all and canonical_tool(name) not in authority.denied
                and not (authority.disable_mcp and name.startswith('mcp__'))}
        self.selected = {'discover_tools'} & self.inventory.keys()
        from . import routing_trace
        routing_trace.catalog(self.inventory.keys())

    def schemas(self):
        return [self.inventory[name] for name in sorted(self.selected)]


def active_catalog(contract=None):
    value = _ACTIVE.get()
    return value if value is not None and (contract is None or any(contract is item for item in value.contracts)) else None


def derived_catalog(original, derived):
    """Preserve request-local selection across a server-derived editor view.

    Only aliases the same policy-filtered catalog, never an arbitrary child
    contract or new inventory. Execution authority remains independently bound.
    """
    catalog = active_catalog(original)
    if catalog is not None and derived.catalog_json == original.catalog_json:
        catalog.contracts.append(derived)


@contextmanager
def bind_catalog(contract, enabled):
    if not enabled or contract is None or active_catalog(contract) is not None:
        yield
        return
    token = _ACTIVE.set(ToolCatalog(contract))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def _classify_missing_names(catalog, names):
    missing = [name for name in names if name not in catalog.inventory]
    if not missing:
        return
    try:
        from .authority import active_request_authority
        from src.turn_contract import canonical_tool
        authority = active_request_authority()
        denied = set()
        if authority is not None:
            denied = {name for name in missing if canonical_tool(name) in authority.denied or authority.block_all}
    except Exception:
        denied = set()
    unknown = [name for name in missing if name not in getattr(catalog, "full_inventory", catalog.inventory)]
    if unknown:
        raise DiscoveryError(
            "unknown_tool",
            "Requested tool is not registered: " + ", ".join(unknown),
            requested=missing,
        )
    if denied:
        raise DiscoveryError(
            "authorization_denied",
            "Requested tool is denied by current authorization: " + ", ".join(sorted(denied)),
            recoverable=False,
            requested=missing,
        )
    raise DiscoveryError(
        "unavailable_tool",
        "Requested tool is not in this request catalog: " + ", ".join(missing),
        requested=missing,
    )


async def discover_tools(content, ctx):
    catalog = active_catalog()
    if catalog is None:
        return _discovery_payload(DiscoveryError(
            "catalog_unavailable",
            "No interactive request catalog is available",
            recoverable=False,
        ))
    try:
        args = json.loads(content)
        if not isinstance(args, dict) or set(args) - {'names', 'query', 'offset'}:
            raise DiscoveryError('invalid_arguments', 'Use names, query, or a catalog offset')
        names = args.get('names')
        retrieval = 'names'
        index_state = None
        if names is None:
            query = args.get('query', '')
            if not isinstance(query, str) or len(query) > 500:
                raise DiscoveryError('invalid_arguments', 'query must be at most 500 characters')
            if query:
                import asyncio
                from src.tool_index import get_tool_index, get_ready_tool_index, get_tool_index_status
                index = get_ready_tool_index()
                status = get_tool_index_status()
                index_state = status.get('state', 'unknown')
                found = None
                try:
                    # Initialization is optional and can be warming or in its
                    # existing failure cooldown. Do not queue more initializers
                    # behind a timed-out/prewarming worker on every discovery.
                    if index is None and status.get('state') != 'warming' and not status.get('retry_after_seconds',0):
                        index = await asyncio.wait_for(asyncio.to_thread(get_tool_index), timeout=5)
                        if index is None:
                            index_state = get_tool_index_status().get('state', 'unavailable')
                    if index is not None:
                        found = await asyncio.wait_for(asyncio.to_thread(index.retrieve, query, 6), timeout=5)
                        index_state = 'ready'
                except Exception:
                    # Semantic retrieval is an optional ranking aid, never an
                    # authority source. The policy-filtered catalog still exists.
                    found = None
                    index_state = 'unavailable'
                if found is not None:
                    retrieval = 'semantic'
                    # retrieve returns ranked matches, unlike the router's
                    # get_tools_for_query which unions an always-on baseline.
                    # Subtracting that baseline erased real read/search hits.
                    names = list(dict.fromkeys(name for name in found if name in catalog.inventory))[:6]
                else:
                    retrieval = 'catalog'
                    words = set(re.findall(r'\w+',query.casefold()))
                    ranked = []
                    for name,schema in catalog.inventory.items():
                        text = name.replace('_',' ')+' '+schema['function'].get('description','')
                        score = len(words & set(re.findall(r'\w+',text.casefold())))
                        if score and name != 'discover_tools':
                            ranked.append((-score,name))
                    names = [name for _,name in sorted(ranked)[:6]]
                    # A workspace inspection request needs viable native search
                    # alternatives even when a query names code concepts rather
                    # than the tools used to locate them. This is schema
                    # relevance only; authority still gates each execution.
                    if ranked and 'workspace_read' in catalog.contract.capabilities:
                        native = [name for name in ('grep', 'glob', 'read_file') if name in catalog.inventory]
                        names = list(dict.fromkeys(native + names))[:6]
            else:
                names = []
        if not isinstance(names, list) or len(names) > 6 or any(not isinstance(n, str) for n in names):
            raise DiscoveryError('invalid_arguments', 'Select at most six registered tool names', requested=[n for n in names if isinstance(n, str)][:6] if isinstance(names, list) else ())
        requested_names = list(names)
        resolved_names = []
        aliases = {}
        for name in names:
            canonical = _DISCOVERY_ALIASES.get(name)
            if canonical and canonical in catalog.inventory:
                aliases[name] = canonical
                resolved_names.append(canonical)
            else:
                resolved_names.append(name)
        names = list(dict.fromkeys(resolved_names))
        _classify_missing_names(catalog, names)
        offset = args.get('offset', 0)
        if type(offset) is not int or not 0 <= offset <= len(catalog.inventory):
            raise DiscoveryError('invalid_arguments', 'Invalid catalog offset')
        if len(catalog.selected | set(names)) > 16:
            raise DiscoveryError('invalid_arguments', 'At most sixteen tools may be discovered in one request')
        catalog.selected.update(names)
        from . import routing_trace
        routing_trace.discover(names)
        entries, used = [], 0
        for name, schema in sorted(catalog.inventory.items())[offset:]:
            row = {'name':name, 'description':schema['function'].get('description','')[:120]}
            size = len(json.dumps(row, ensure_ascii=False))
            if used + size > 2200:
                break
            entries.append(row)
            used += size
        return {'output':json.dumps({'selected':names, 'retrieval':retrieval, 'semantic_index':index_state, 'authorization':'Selection exposes schemas only. Missing permission requires user approval; policy denials remain final.',
            'tools':entries, 'resolved_aliases':aliases,
            'next_offset':offset + len(entries) if offset + len(entries) < len(catalog.inventory) else None}, ensure_ascii=False), 'exit_code':0}
    except DiscoveryError as error:
        error.catalog = catalog
        from . import routing_trace
        routing_trace.discover((), refused=getattr(error, 'requested', ()), code=getattr(error, 'code', None))
        return _discovery_payload(error)
    except KeyError as error:
        return _discovery_payload(DiscoveryError('invalid_schema', 'Tool schema is missing required fields', recoverable=False))
    except (ValueError, TypeError, TimeoutError) as error:
        return _discovery_payload(DiscoveryError('invalid_arguments', str(error)))
    except Exception:
        logger.error("discover_tools internal failure")
        return _discovery_payload(DiscoveryError(
            'internal_discovery_failure',
            'Tool discovery failed internally',
            recoverable=False,
        ))
