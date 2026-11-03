# WEB_VISIT_LEDGER

**Path:** `src/agent_runtime/web_visit_ledger.py`

## Purpose

Keep a turn-local record of `web_fetch` visits so prompt compaction cannot erase the fact that a URL was already read. Distinguishes partial, focused, and complete fetches while still allowing legitimate upgrades and new extraction goals.

## Behavior

| Request | Prior visit | Result |
|---|---|---|
| Same kind / same goal | present | suppressed as duplicate |
| `full=true` / complete | partial | allowed upgrade |
| New `extraction_goal` | complete or focused | allowed additional evidence |
| Weaker re-read | stronger visit | suppressed |

Normalization drops URL fragments and lowercases hostnames; query strings remain so paginated API reads stay distinct. Mixed `urls` batches are filtered per URL: already-complete duplicates are skipped while new or upgradeable targets still fetch.
