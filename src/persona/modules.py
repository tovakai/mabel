"""Phase 01: validated, hot-loadable CORE / FUNCTION / MOOD Markdown modules.

Modules live in Mabel's vault (``identity/`` by default: Syncthing-replicated,
Git-ignored, editable in Obsidian). They describe identity, working style and
expression; they carry no authority. A module that fails validation is
rejected with a reason and the last known good version of that module (if any)
keeps serving. Content is re-read when a file's size or mtime changes, so an
edit applies on the next request without a restart.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

import yaml

BODY_LIMITS = {"core": 6000, "function": 1200, "mood": 600}
_ID = {
    "core": re.compile(r"^core$"),
    "function": re.compile(r"^function\.[a-z][a-z0-9_]{1,31}$"),
    "mood": re.compile(r"^mood\.[a-z][a-z0-9_]{1,31}$"),
}
_FOLDER = {"core": "", "function": "functions", "mood": "moods"}
# Modules shape voice and working style; they never change permissions.
_AUTHORITY_CONFLICT = re.compile(
    r"\b(?:ignore|disregard|override)\s+(?:all\s+|any\s+)?(?:previous|prior|system|safety|above)\b"
    r"|\b(?:disable|bypass|skip)\s+(?:the\s+)?(?:approval|approvals|safety|permission|permissions|sandbox)\b"
    r"|\byou\s+(?:may|can|are\s+allowed\s+to)\s+(?:now\s+)?(?:run|execute|delete|access|write)\s+(?:anything|any\s+file|without)\b"
    r"|\bgrant(?:ed)?\s+(?:yourself|full|unrestricted)\b",
    re.I,
)


class ModuleError(ValueError):
    pass


@dataclass(frozen=True)
class Module:
    id: str
    kind: str
    version: int
    path: str
    sha: str
    body: str
    meta: dict = field(default_factory=dict)

    @property
    def ref(self) -> str:
        """Opaque, ledger-safe reference: id@version:hash-prefix."""
        return f"{self.id}@{self.version}:{self.sha[:12]}"


def parse_module(text: str, *, kind: str, path: str) -> Module:
    if not text.startswith("---\n"):
        raise ModuleError("missing YAML frontmatter")
    try:
        _, front, body = text.split("---\n", 2)
    except ValueError:
        raise ModuleError("unterminated YAML frontmatter") from None
    try:
        meta = yaml.safe_load(front) or {}
    except yaml.YAMLError as error:
        raise ModuleError(f"invalid YAML: {str(error).splitlines()[0][:80]}") from None
    if not isinstance(meta, dict):
        raise ModuleError("frontmatter must be a mapping")
    module_id = str(meta.get("id") or "")
    if meta.get("kind") != kind:
        raise ModuleError(f"kind must be {kind!r}")
    if not _ID[kind].fullmatch(module_id):
        raise ModuleError(f"invalid {kind} id {module_id!r}")
    version = meta.get("version")
    if type(version) is not int or version < 1:
        raise ModuleError("version must be a positive integer")
    if meta.get("status") not in {"active", "draft", "retired"}:
        raise ModuleError("status must be active, draft or retired")
    body = body.strip()
    if not body:
        raise ModuleError("empty body")
    if len(body) > BODY_LIMITS[kind]:
        raise ModuleError(f"body over {BODY_LIMITS[kind]} characters")
    conflict = _AUTHORITY_CONFLICT.search(body)
    if conflict:
        raise ModuleError(f"instruction conflicts with runtime policy: {conflict.group(0)!r}")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Module(module_id, kind, version, path, sha, body,
                  {k: v for k, v in meta.items() if k not in {"id", "kind", "version"}})


@dataclass
class Library:
    """Active modules plus validation problems, as of the last scan."""
    core: Module | None
    functions: dict[str, Module]
    moods: dict[str, Module]
    errors: list[dict]


class ModuleStore:
    """Scans the module folder; caches by (size, mtime) and keeps last-known-good."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = Lock()
        self._cache: dict[str, tuple[tuple[int, int], Module | None, str | None]] = {}
        self._last_good: dict[str, Module] = {}

    def _files(self) -> list[tuple[str, Path]]:
        found = []
        core = self.root / "core.md"
        if core.exists():
            found.append(("core", core))
        for kind in ("function", "mood"):
            folder = self.root / _FOLDER[kind]
            if folder.is_dir() and not folder.is_symlink():
                found.extend((kind, path) for path in sorted(folder.glob("*.md")))
        return found

    def load(self) -> Library:
        with self._lock:
            core, functions, moods, errors = None, {}, {}, []
            seen_ids: set[str] = set()
            root = self.root.resolve() if self.root.exists() else self.root
            for kind, path in self._files():
                key = str(path)
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        raise ModuleError("symlink or path outside the module folder")
                    stat = path.stat()
                    stamp = (stat.st_size, stat.st_mtime_ns)
                    if stat.st_size > BODY_LIMITS[kind] + 2000:
                        raise ModuleError("file too large")
                    cached = self._cache.get(key)
                    if cached and cached[0] == stamp:
                        module, problem = cached[1], cached[2]
                    else:
                        try:
                            module, problem = parse_module(path.read_text(encoding="utf-8"),
                                                           kind=kind, path=key), None
                        except ModuleError as error:
                            module, problem = None, str(error)
                        self._cache[key] = (stamp, module, problem)
                    if problem:
                        raise ModuleError(problem)
                except (OSError, UnicodeError, ModuleError) as error:
                    fallback = self._last_good.get(key)
                    errors.append({"path": key, "kind": kind, "error": str(error)[:200],
                                   "serving": fallback.ref if fallback else None})
                    module = fallback
                    if module is None:
                        continue
                if module.meta.get("status") != "active":
                    continue
                if module.id in seen_ids:
                    errors.append({"path": key, "kind": kind, "error": f"duplicate id {module.id}",
                                   "serving": None})
                    continue
                seen_ids.add(module.id)
                self._last_good[key] = module
                if kind == "core":
                    core = module
                elif kind == "function":
                    functions[module.id.split(".", 1)[1]] = module
                else:
                    moods[module.id.split(".", 1)[1]] = module
            return Library(core, functions, moods, errors)
