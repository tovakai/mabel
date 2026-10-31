"""Logical path confinement for POSIX and Windows remote styles."""

import pytest

from src.fs_devices.path_ops import (
    confine_logical,
    detect_path_style,
    join_path,
    normalize_remote_path,
    parent_path,
)


def test_detect_windows_style():
    assert detect_path_style(r"C:\Users\tovakai") == "windows"
    assert detect_path_style("/home/tovakai") == "posix"


def test_confine_posix_relative_and_escape():
    assert confine_logical("/data/vault", "notes/a.md", style="posix") == "/data/vault/notes/a.md"
    with pytest.raises(ValueError):
        confine_logical("/data/vault", "../outside", style="posix")
    with pytest.raises(ValueError):
        confine_logical("/data/vault", "/etc/passwd", style="posix")


def test_confine_windows_paths():
    root = r"C:\Media"
    assert confine_logical(root, r"Movies\x.mkv", style="windows").lower().endswith(r"movies\x.mkv")
    with pytest.raises(ValueError):
        confine_logical(root, r"..\Windows", style="windows")


def test_normalize_drops_dotdot_at_root():
    assert normalize_remote_path("/a/../b", style="posix") == "/b"
    assert normalize_remote_path("/../b", style="posix") == "/b"


def test_parent_and_join():
    assert parent_path("/a/b", style="posix") == "/a"
    assert join_path("/a", "b", "c", style="posix") == "/a/b/c"
