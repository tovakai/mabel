"""Contract tests for the co-located Mabel Git + Obsidian + Syncthing root.

These do not require a running Syncthing daemon. Real Syncthing behavior
must still be checked on a disposable vault before pairing live devices.
"""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = ("memory", "identity", "knowledge", "projects", "journal", "attachments")


@pytest.mark.skipif(shutil.which("git") is None, reason="git executable required")
@pytest.mark.parametrize(
    "relative_path",
    [
        *(f"{directory}/private.md" for directory in PRIVATE_DIRS),
        *(f"{directory}/private.txt" for directory in PRIVATE_DIRS),
        ".obsidian/plugins/odysseus/data.json",
        ".obsidian/workspace.json",
        "data/private.db",
        "logs/runtime.log",
    ],
)
def test_private_vault_files_never_enter_public_git_by_default(relative_path):
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", relative_path],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, f"Expected Git to ignore {relative_path}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git executable required")
@pytest.mark.parametrize("directory", PRIVATE_DIRS)
def test_gitkeep_scaffolding_is_not_ignored(directory):
    marker = ROOT / directory / ".gitkeep"
    assert marker.is_file()
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", f"{directory}/.gitkeep"],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 1, f"Expected {marker} to be Git-addable"


def test_syncthing_root_is_a_top_level_content_allowlist():
    lines = [
        line.strip()
        for line in (ROOT / ".stignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    ]
    assert lines[-1] == "*"
    for directory in PRIVATE_DIRS:
        parent = f"!/{directory}"
        recursive = f"!/{directory}/**"
        assert parent in lines and recursive in lines
        assert lines.index(parent) < lines.index(recursive) < len(lines) - 1

    # First Syncthing match wins; these protections must precede the includes.
    first_include = next(i for i, line in enumerate(lines) if line.startswith("!"))
    for protected in (".env", "secrets.env", "*.db", "*.sqlite", "*.sqlite3", "*.pem", "*.key"):
        assert protected in lines
        assert lines.index(protected) < first_include


def test_private_notes_excluded_from_docker_build_context():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for directory in PRIVATE_DIRS:
        assert f"/{directory}/" in text
    assert "/.obsidian/" in text
