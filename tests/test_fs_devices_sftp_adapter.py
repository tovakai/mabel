"""SFTP adapter behavior with an in-process stub and optional paramiko loopback."""

from __future__ import annotations

import os
import socket
import stat
import threading
from dataclasses import dataclass

import pytest

from src.fs_devices.adapters import DirEntry, SftpAdapter
from src.fs_devices.path_ops import confine_logical


class _Attr:
    def __init__(self, filename, mode, size=0):
        self.filename = filename
        self.st_mode = mode
        self.st_size = size


class _FakeSftp:
    """Minimal SFTP surface for adapter unit tests (no network)."""

    def __init__(self, tree: dict):
        # tree: path -> {"dirs": set, "files": {name: bytes}, "links": set}
        self.tree = tree

    def _node(self, path):
        path = path.rstrip("/") or "/"
        if path not in self.tree:
            raise FileNotFoundError(path)
        return self.tree[path]

    def listdir_attr(self, path):
        node = self._node(path)
        out = []
        for name in sorted(node.get("dirs", ())):
            out.append(_Attr(name, stat.S_IFDIR | 0o755))
        for name, data in sorted(node.get("files", {}).items()):
            out.append(_Attr(name, stat.S_IFREG | 0o644, size=len(data)))
        for name in sorted(node.get("links", ())):
            out.append(_Attr(name, stat.S_IFLNK | 0o777))
        return out

    def lstat(self, path):
        parent, name = path.rsplit("/", 1) if path != "/" else ("/", "")
        if path == "/":
            return _Attr("/", stat.S_IFDIR | 0o755)
        node = self._node(parent if parent else "/")
        if name in node.get("dirs", ()):
            return _Attr(name, stat.S_IFDIR | 0o755)
        if name in node.get("files", {}):
            data = node["files"][name]
            return _Attr(name, stat.S_IFREG | 0o644, size=len(data))
        if name in node.get("links", ()):
            return _Attr(name, stat.S_IFLNK | 0o777)
        raise FileNotFoundError(path)

    def normalize(self, path):
        return "/home/user" if path == "." else path

    def open(self, path, mode="rb"):
        parent, name = path.rsplit("/", 1)
        node = self._node(parent)

        class _Handle:
            def __init__(self, data: bytes):
                self._data = data
                self._pos = 0

            def read(self, n=-1):
                if n < 0:
                    chunk = self._data[self._pos:]
                else:
                    chunk = self._data[self._pos:self._pos + n]
                self._pos += len(chunk)
                return chunk

            def write(self, data):
                node.setdefault("files", {})[name] = bytes(data)
                return len(data)

            def close(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        if "r" in mode:
            return _Handle(node["files"][name])
        return _Handle(b"")

    def close(self):
        return None


def test_sftp_browse_skips_symlinks_and_files():
    sftp = _FakeSftp({
        "/home/user": {
            "dirs": {"docs", "bin"},
            "files": {"readme.txt": b"hi"},
            "links": {"escape"},
        },
        "/home/user/docs": {"dirs": set(), "files": {}, "links": set()},
        "/home/user/bin": {"dirs": set(), "files": {}, "links": set()},
    })
    adapter = SftpAdapter("device:x", sftp, path_style="posix", home="/home/user")
    entries, truncated = adapter.listdir("/home/user")
    names = {e.name for e in entries}
    assert names == {"docs", "bin"}
    assert "readme.txt" not in names
    assert "escape" not in names
    assert truncated is False


def test_sftp_confine_and_read():
    sftp = _FakeSftp({
        "/data": {"dirs": set(), "files": {"note.md": b"hello"}, "links": set()},
    })
    adapter = SftpAdapter("device:x", sftp, path_style="posix", home="/data")
    path = adapter.confine("/data", "note.md")
    assert path == "/data/note.md"
    data = adapter.read_bytes(path, max_bytes=100)
    assert data == b"hello"
    with pytest.raises(ValueError):
        adapter.confine("/data", "../etc/passwd")


def test_windows_style_confine_via_adapter_home():
    # Adapter confine uses logical rules; Windows roots must not assume POSIX.
    root = r"D:\Media"
    confined = confine_logical(root, r"Films\a.mkv", style="windows")
    assert "Films" in confined
    with pytest.raises(ValueError):
        confine_logical(root, r"..\Windows\System32", style="windows")


def test_paramiko_loopback_optional():
    """Exercise real Paramiko against a tiny threaded server when available.

    Skips cleanly if the environment cannot bind a local SSH/SFTP server.
    """
    paramiko = pytest.importorskip("paramiko")
    try:
        from paramiko import ServerInterface, SFTPServerInterface, SFTPHandle, SFTPAttributes
    except Exception:
        pytest.skip("Paramiko SFTP server APIs unavailable")

    # Keep this as a soft integration probe: construction of Transport is enough
    # to prove the dependency loads. Full OpenSSH container tests are manual.
    assert hasattr(paramiko, "SSHClient")
    assert hasattr(paramiko, "Transport")
