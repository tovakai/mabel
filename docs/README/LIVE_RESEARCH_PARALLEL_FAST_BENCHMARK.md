# LIVE_RESEARCH_PARALLEL_FAST_BENCHMARK.md

Script: `tests/live_research_parallel_fast_benchmark.py`

## Purpose

Bounded live chat-API check that default `research_parallel(mode=both, vault_strategy=fast)`
returns web evidence and vault index candidates without waiting for deep
`dispatch_dottie`. Compares against known deep-timeout traces; does not re-run a
120s deep job.

## Usage

```bash
cd ~/Documents/mabel
.venv-arch/bin/python tests/live_research_parallel_fast_benchmark.py
```

Writes `/tmp/mabel-parallel-fast-benchmark.json`.
