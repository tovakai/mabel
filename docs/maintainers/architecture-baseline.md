# Phase 0 architecture baseline

This map describes the existing runtime on zum before Phase 0 integration.

| Surface | Code | Existing responsibility |
| --- | --- | --- |
| Chat context | `routes/chat_helpers.py:build_chat_context` | History, preprocessing, compaction and request context. |
| Prompt assembly | `src/agent_loop.py:_build_system_prompt` | Builds the agent system message and bounded context for each model route. |
| Agent turn | `routes/chat_routes.py`, `src/agent_loop.py:stream_agent_loop` | Constructs request authority and turn contract, dispatches tools and continues the model. |
| Model transport | `src/llm_core.py:stream_llm_with_fallback`, `src/generation_budget.py` | Streaming requests, context budgeting and bounded provider recovery. |
| Discovery | `src/agent_runtime/tool_catalog.py:discover_tools`, `src/tool_index.py:get_tool_index` | Request-local policy-filtered catalog; optional semantic index and catalog fallback. |
| Readiness | `src/readiness.py:check_readiness`, `app.py` `/api/ready` | Reports tool-index lifecycle without a model call; endpoint requires authentication. |
| Authorization | `src/agent_runtime/authority.py:RequestAuthority`, `src/turn_contract.py:resolve_turn_contract`, `src/tool_execution.py` | Request grants, policy and resource checks remain separate from tool schemas. |
| Scheduled tasks | `core/database.py:ScheduledTask`, `TaskRun`, `src/task_scheduler.py:TaskScheduler` | Persistent scheduled tasks and run history; no general lease/checkpoint contract. |
| Background results | `src/background_tool_jobs.py:BackgroundToolJobs` | Durable chat result delivery; not a generic exclusive claim and checkpoint store. |
| Task artifacts | `src/agent_runtime/task_artifacts.py` | Bounded session-scoped in-memory results, not durable job state. |
| Dottie | `src/knowledge_engine/research.py`, `src/knowledge_engine/tools.py` | Bounded delegated read-only research with source validation. |
| Vault index | `src/knowledge_engine/index.py:VaultIndex` | Read-only Markdown source and locally rebuildable SQLite index outside the vault. |

A normal agent turn enters `routes/chat_routes.py`, builds context and server-owned authority, resolves the turn contract, then enters `stream_agent_loop`. Its route-specific prompt and schemas go to `stream_llm_with_fallback`; tool calls pass through the dispatcher and authorization/resource checks before results return to the next model request. Session persistence remains in the existing chat route and session manager.

Baseline on zum: `tests/test_discovery_context_regression.py`, `tests/test_tool_index_lifecycle.py`, and `tests/test_readiness.py` passed 25 tests with 6 warnings on 2026-10-29. An unauthenticated `/api/ready` request returned 401, as expected for its protected endpoint. Further Phase 0 implementation and validation remain pending.
