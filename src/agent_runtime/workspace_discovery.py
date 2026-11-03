"""Conventional control/history directories, not a blanket hidden-name filter."""
from fnmatch import fnmatchcase
from functools import lru_cache

CONTROL_DIRECTORIES = frozenset({
    '.stversions', '.stfolder', '.git', '.hg', '.svn', '.obsidian',
    '.trash', '.cache', '__pycache__', 'node_modules', '.venv',
})


def is_control_directory(name):
    return name.casefold() in CONTROL_DIRECTORIES


def matches_glob(parts, pattern):
    """Path glob matching where * stays within a component and ** recurses."""
    patterns = tuple(pattern.replace('\\', '/').split('/'))
    @lru_cache(maxsize=None)
    def match(i, j):
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == '**':
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatchcase(parts[i], patterns[j]) and match(i + 1, j + 1)
    return match(0, 0)
