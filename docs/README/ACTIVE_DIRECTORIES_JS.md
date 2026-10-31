# ACTIVE_DIRECTORIES_JS

**Path:** `static/js/activeDirectories.js`

## Purpose

Composer multi-select for per-conversation **Active Directories**. A preferred local working directory for shell/Python is optional and is not required for tool use. Persists via `PATCH /session/{id}` (`active_directories`, `primary_local_directory_id`) and appends the same fields to chat FormData.

Reuses the former workspace indicator pill / overflow entry with updated terminology.
