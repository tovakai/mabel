"""Explicit local index maintenance; never launched at app startup."""
import argparse
import json
import os
import time
from .index import VaultIndex


def watch(index, *, interval=0.5, reconcile_seconds=60, stop=None, after_scan=None):
    """Portable polling observer: coalesce changes, periodically reconcile hashes."""
    previous, dirty_since, last_full = None, None, 0.
    while stop is None or not stop.is_set():
        stamp=[]
        for path in index.discover():
            try:
                stat=(index.root/path).stat()
                stamp.append((path,stat.st_mtime_ns,stat.st_size))
            except OSError:
                # An editor/synchronizer may remove a file during observation.
                continue
        clock = time.monotonic()
        if stamp != previous:
            previous, dirty_since = stamp, clock
        if clock-last_full>=reconcile_seconds or dirty_since is not None and clock-dirty_since>=interval:
            index.reconcile()
            if after_scan is not None:
                after_scan()
            last_full, dirty_since = clock, None
        if stop is None:
            time.sleep(interval)
        elif stop.wait(interval):
            return


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault',required=True)
    parser.add_argument('--cache-dir')
    parser.add_argument('--owner',default='')
    parser.add_argument('--exclude',action='append',default=[])
    parser.add_argument('--include',action='append',default=[])
    parser.add_argument('--minimum-tokens',type=int,default=150)
    parser.add_argument('--maximum-tokens',type=int,default=800)
    parser.add_argument('--relationship-key',action='append',default=None)
    parser.add_argument('--edge-weights',default='{"property":3,"inline":2,"embed":1}')
    sub=parser.add_subparsers(dest='command',required=True)
    scan=sub.add_parser('scan')
    scan.add_argument('--embed',action='store_true')
    scan.add_argument('--embedding-limit',type=int,default=1000)
    sub.add_parser('embedding-coverage')
    embed=sub.add_parser('embed',help='Index missing vectors through the configured remote Knowledge embeddings role; no source scan')
    embed.add_argument('--embedding-limit',type=int,default=256)
    embed.add_argument('--batch-size',type=int,default=8)
    rebuild=sub.add_parser('rebuild-vectors')
    rebuild.add_argument('space_id')
    sub.add_parser('status')
    watcher=sub.add_parser('watch')
    watcher.add_argument('--embed',action='store_true')
    watcher.add_argument('--embedding-limit',type=int,default=1000)
    watcher.add_argument('--interval',type=float,default=0.5)
    watcher.add_argument('--reconcile-seconds',type=float,default=60)
    search=sub.add_parser('search')
    search.add_argument('query',nargs='?',default='')
    search.add_argument('--mode',choices=['lexical','metadata_only','semantic','hybrid'],default='lexical')
    search.add_argument('--rerank',action='store_true')
    search.add_argument('--filters',default='{}')
    search.add_argument('--path-prefix',default='')
    graph=sub.add_parser('graph')
    graph.add_argument('note_id')
    read=sub.add_parser('read')
    read.add_argument('note_id')
    read.add_argument('--start',type=int,default=1)
    read.add_argument('--end',type=int,default=40)
    sub.add_parser('benchmark')
    args=parser.parse_args()
    index=VaultIndex(args.vault,owner=args.owner,cache_dir=args.cache_dir,exclusions=args.exclude,inclusions=args.include,
        minimum_tokens=args.minimum_tokens,maximum_tokens=args.maximum_tokens,
        relationship_keys=args.relationship_key,edge_weights=json.loads(args.edge_weights))
    if args.command=='scan':
        result=index.reconcile()
        if args.embed:
            import asyncio
            from .semantic import index_embeddings
            result['embeddings']=asyncio.run(index_embeddings(index,owner=args.owner,limit=args.embedding_limit))
    elif args.command=='embedding-coverage':
        from .semantic import coverage
        result=coverage(index)
    elif args.command=='embed':
        import asyncio
        from .maintenance import embed_pending
        result=asyncio.run(embed_pending(index,owner=args.owner,limit=args.embedding_limit,batch_size=args.batch_size))
    elif args.command=='rebuild-vectors':
        if not args.space_id or len(args.space_id)!=64:
            parser.error('Use the exact space ID from embedding-coverage')
        with index.connect() as db:
            db.execute('DELETE FROM chunk_vectors WHERE space_id=?',(args.space_id,))
        result={'invalidated_space':args.space_id,'next':'Run scan --embed explicitly to rebuild compatible vectors'}
    elif args.command=='watch':
        if args.interval<0.05 or args.reconcile_seconds<args.interval:
            parser.error('Use interval >= 0.05 and reconciliation >= interval')
        def embed_changed():
            if args.embed:
                import asyncio
                from .semantic import index_embeddings
                print(json.dumps(asyncio.run(index_embeddings(index,owner=args.owner,limit=args.embedding_limit))))
        try:
            watch(index,interval=args.interval,reconcile_seconds=args.reconcile_seconds,after_scan=embed_changed)
        except KeyboardInterrupt:
            pass
        return
    elif args.command=='status':
        result=index.status()
    elif args.command=='search':
        import asyncio
        from .semantic import search as retrieve
        result=asyncio.run(retrieve(index,args.query,mode=args.mode,property_filters=json.loads(args.filters),
            path_prefix=args.path_prefix,use_reranking=args.rerank,owner=args.owner))
    elif args.command=='graph':
        result=index.traverse(args.note_id)
    elif args.command=='read':
        result=index.read_range(args.note_id,args.start,args.end)
    else:
        start=time.perf_counter()
        result={'scan':index.reconcile(),'scan_seconds':time.perf_counter()-start}
        start=time.perf_counter()
        index.search('the')
        result['lexical_seconds']=time.perf_counter()-start
        try:
            import resource
            result['peak_rss_platform_units']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:
            result['peak_rss_platform_units']=None
        result['gpu_benchmark']='not_run'
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
