"""Explicit synthetic inference benchmarks; never downloads or launches models."""
import argparse
import asyncio
import json
import statistics
import time
import httpx
from dataclasses import asdict
from src import model_roles as roles
from .semantic import embed,rerank


async def benchmark(role,owner='',runs=3):
    measurements=[]
    errors=[]
    for i in range(runs):
        started=time.perf_counter()
        try:
            if role=='embedding':
                value,diagnostic,route=await roles.run_candidates(role,roles.candidates(role,owner),
                    lambda route:embed(route,['Synthetic snap-fit lid width: 62 mm.','Svensk text om mått och tolerans.']),
                    owner=owner,required='embedding')
                size=len(value)
            elif role=='reranking':
                value,diagnostic=await rerank('snap-fit lid dimensions',[
                    {'chunk_id':str(n),'excerpt':text} for n,text in enumerate(
                        ['Lid width: 62 mm.','En svensk anteckning om filmen.','Clearance: 0.2 mm.']*4)],owner=owner)
                size=len(value)
            else:
                value=await roles.call(role,[{'role':'user','content':'Reply with only: benchmark ready'}],
                    owner=owner,max_tokens=24)
                diagnostic=roles.diagnostics(owner)[role]
                size=len(value)
            measurement={'sample':i,'seconds':time.perf_counter()-started,'items':size,'route':diagnostic}
            if role=='reranking':
                measurement['synthetic_top1_relevant']=bool(value and int(value[0]['chunk_id'])%3==0)
            measurements.append(measurement)
        except (roles.RoleUnavailable,ValueError,RuntimeError,httpx.HTTPError) as error:
            errors.append({'sample':i,'error':type(error).__name__})
    durations=[r['seconds'] for r in measurements]
    return {'role':role,'samples':measurements,'errors':errors,
        'first_request_seconds':durations[0] if durations else None,
        'warm_median_seconds':statistics.median(durations[1:]) if len(durations)>1 else None,
        'gpu_memory_measured':False,'cold_start_verified':False,
        'note':'First client request is not proof of a cold model load. Measure server/GPU memory separately.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role',choices=sorted(roles.ROLE_CAPABILITIES))
    parser.add_argument('--owner',default='')
    parser.add_argument('--runs',type=int,default=3)
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--stack',action='store_true',help='Concurrently exercise Research, Embedding and Reranking using synthetic input')
    args=parser.parse_args()
    if not 1<=args.runs<=20:
        parser.error('runs must be 1–20')
    if args.stack:
        async def stack():
            started=time.perf_counter()
            results=await asyncio.gather(*(benchmark(role,args.owner,args.runs) for role in ('research','embedding','reranking')))
            return {'simultaneous_roles':results,'elapsed_seconds':time.perf_counter()-started,
                'gpu_memory_measured':False,'server_loading_policy':'controlled by configured inference servers'}
        print(json.dumps(asyncio.run(stack()),ensure_ascii=False,indent=2))
    elif args.status:
        print(json.dumps({'roles':roles.diagnostics(args.owner),'health':roles.health_status(args.owner),
            'configured_roles':list(roles.policies(args.owner))},indent=2))
    elif args.role:
        print(json.dumps(asyncio.run(benchmark(args.role,args.owner,args.runs)),ensure_ascii=False,indent=2))
    else:
        parser.error('Choose --status or --role')


if __name__=='__main__':
    main()
