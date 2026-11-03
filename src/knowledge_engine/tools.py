"""Read-only tools reached through the existing native resource dispatcher."""
import asyncio
import json
import sqlite3
from .index import VaultIndex

TOOLS = frozenset({'vault_knowledge_search','vault_traverse_graph','vault_read_range','vault_index_status','vault_retrieve_batch','dispatch_dottie'})


async def execute(name, content, ctx):
    from src.agent_runtime.resource_binding import active_resource_operation
    try:
        bound = active_resource_operation()
        if bound is None or bound.operation.tool != name or len(bound.bindings) != 1:
            raise ValueError('Knowledge retrieval requires an admitted sealed workspace')
        bound.validate()
        binding = bound.bindings[0]
        from src.agent_runtime.resources import FilesystemResource
        if not isinstance(binding.resource,FilesystemResource):
            raise ValueError('Knowledge indexing requires an authorized local vault copy; remote roots are not indexed')
        # A child can index its own narrowed root; it cannot consult a parent
        # database whose cached text would extend beyond that root.
        root = binding.resource.path
        args = json.loads(content)
        if not isinstance(args,dict):
            raise ValueError('Tool arguments must be an object')
        from .research import active_budget, batch, dispatch
        budget = active_budget()
        if name == 'dispatch_dottie':
            return await dispatch(content,ctx)
        if name == 'vault_retrieve_batch':
            return await batch(content,ctx)
        args.pop('path',None)  # Dispatcher-resolved root, never a model cache path.
        owner=ctx['request_authority'].owner
        index = VaultIndex(root,owner=owner)
        if budget is not None:
            if name == 'vault_traverse_graph':
                budget.check_graph(args,index)
            if name == 'vault_knowledge_search':
                args['top_k']=min(args.get('top_k',8),max(1,budget.max_chunks-budget.chunks))
            elif name == 'vault_traverse_graph':
                args['limit']=min(args.get('limit',10),max(1,budget.max_chunks-budget.chunks))
        source_depth=args.pop('_source_depth',None)
        if name == 'vault_index_status':
            if set(args)-{'refresh'} or type(args.get('refresh',False)) is not bool:
                raise ValueError('Status accepts only boolean refresh')
            result = await asyncio.to_thread(index.reconcile if args.get('refresh') else index.status)
        else:
            if not index.status()['last_scan']:
                await asyncio.to_thread(index.reconcile)
            if name == 'vault_knowledge_search':
                if set(args)-{'query','mode','property_filters','path_prefix','top_k','offset','use_reranking'}:
                    raise ValueError('Unknown knowledge search argument')
                from .semantic import search
                result = await search(index,owner=owner,**args)
            elif name == 'vault_traverse_graph':
                if set(args)-{'note_id','direction','edge_type','search_filter','limit','offset'}:
                    raise ValueError('Unknown graph argument')
                result = await asyncio.to_thread(index.traverse,**args)
            else:
                if set(args)-{'note_id','start_line','end_line'}:
                    raise ValueError('Unknown source range argument')
                result = await asyncio.to_thread(index.read_range,**args)
        if budget is not None:
            budget.observe(name,result,{**args,'_source_depth':source_depth or 0})
        bound.validate()
        # The indexed read API also exposes a line-numbered rendering for its
        # callers. Sending both renderings to the model repeats the complete
        # source text in every subsequent request. The original text and exact
        # start/end lines remain available for source citation and validation.
        if name == 'vault_read_range' and isinstance(result, dict):
            result = {key: value for key, value in result.items() if key != 'line_numbered'}
        return {'output':json.dumps(result,ensure_ascii=False,allow_nan=False),'exit_code':0}
    except (ValueError,TypeError,OSError,RuntimeError,sqlite3.Error) as error:
        return {'error':str(error),'exit_code':1,'executed':False,'failure_kind':'knowledge_retrieval_failed'}


def handler(name):
    async def run(content,ctx):
        return await execute(name,content,ctx)
    return run


def schemas():
    common={'path':{'type':'string','description':'Optional directory within the selected workspace; default is its sealed root.'}}
    def make(name,description,properties,required=()):
        return {'type':'function','function':{'name':name,'description':description,
            'parameters':{'type':'object','additionalProperties':False,'properties':{**common,**properties},'required':list(required)}}}
    result = [
        make('vault_knowledge_search','Search Obsidian Markdown knowledge (not passwords). Lexical or typed metadata filters; returns source lines and bounded explicit graph context. Markdown remains unchanged.',
            {'query':{'type':'string','maxLength':500},'mode':{'type':'string','enum':['lexical','metadata_only','semantic','hybrid']},
             'property_filters':{'type':'object'},'use_reranking':{'type':'boolean'},'path_prefix':{'type':'string'},
             'top_k':{'type':'integer','minimum':1,'maximum':50},'offset':{'type':'integer','minimum':0,'maximum':10000}}),
        make('vault_traverse_graph','Follow explicit wikilinks/frontmatter links or backlinks in the scoped Obsidian index. Unresolved/ambiguous links are labeled. Use next_offset for bounded pages.',
            {'note_id':{'type':'string','description':'Native note ID or vault-relative Markdown path'},
             'direction':{'type':'string','enum':['in','out','both']},'edge_type':{'type':['string','null'],'enum':['inline','property','embed',None]},
             'search_filter':{'type':['string','null']},'limit':{'type':'integer','minimum':1,'maximum':50},
             'offset':{'type':'integer','minimum':0,'maximum':10000}},['note_id']),
        make('vault_read_range','Read current Markdown source lines by native note ID/path. Reports content hash and stale-index status. Read at most 200 lines and 16,000 characters.',
            {'note_id':{'type':'string'},'start_line':{'type':'integer','minimum':1},'end_line':{'type':'integer','minimum':1}},['note_id']),
        make('vault_index_status','Inspect local Obsidian index health/coverage. refresh=true reconciles the disposable cache without modifying Markdown. No inference service is needed.',
            {'refresh':{'type':'boolean'}})]
    result.extend([
        make('vault_retrieve_batch','Execute one to six independent authorized knowledge reads concurrently. Each item enters the existing dispatcher. No writes or nested batches.',
            {'operations':{'type':'array','minItems':1,'maxItems':6,'items':{
                'type':'object','additionalProperties':False,'required':['tool','arguments'],
                'properties':{'tool':{'type':'string','enum':['vault_knowledge_search','vault_traverse_graph','vault_read_range']},
                              'arguments':{'type':'object'}}}}},['operations']),
        make('dispatch_dottie','Delegate bounded, read-only Obsidian research to the optional configured Research model. Returns current-source-validated findings or explicit partial/offline status. Never grants child permissions or writes vault notes.',
            {'research_objective':{'type':'string','minLength':1,'maxLength':2000},
             'constraints':{'type':'object','additionalProperties':False,'properties':{
                 'path_prefix':{'type':['string','null']},'required_sources':{'type':'boolean'},
                 'max_hops':{'type':'integer','minimum':0,'maximum':3}}}},['research_objective'])])
    return result
