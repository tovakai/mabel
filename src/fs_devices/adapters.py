"""Local and SFTP filesystem adapters behind one operation surface."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Protocol

from src.fs_devices.path_ops import (
    confine_logical,
    detect_path_style,
    join_path,
    local_style,
    normalize_remote_path,
    parent_path,
)
from src.path_confinement import PathEscape, confine


@dataclass(frozen=True)
class DirEntry:
    name: str
    path: str
    is_dir: bool
    is_symlink: bool = False


@dataclass(frozen=True)
class FileStat:
    path: str
    is_dir: bool
    is_file: bool
    size: int
    is_symlink: bool = False


class FilesystemAdapter(Protocol):
    device_id: str
    path_style: str

    def listdir(self, path: str, *, max_entries: int = 500) -> tuple[list[DirEntry], bool]:
        ...

    def stat(self, path: str) -> FileStat:
        ...

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        ...

    def write_bytes(self, path: str, data: bytes, *, exclusive: bool = False) -> None:
        ...

    def confine(self, root: str, candidate: str) -> str:
        ...

    def parent(self, path: str) -> str | None:
        ...

    def close(self) -> None:
        ...


class LocalAdapter:
    def __init__(self, device_id: str = "device:local"):
        self.device_id = device_id
        self.path_style = local_style()

    def confine(self, root: str, candidate: str) -> str:
        try:
            return confine(root, candidate)
        except PathEscape as exc:
            raise ValueError(str(exc)) from exc

    def parent(self, path: str) -> str | None:
        target = os.path.realpath(path)
        parent = os.path.dirname(target)
        if not parent or parent == target:
            return None
        return parent

    def listdir(self, path: str, *, max_entries: int = 500) -> tuple[list[DirEntry], bool]:
        target = os.path.realpath(os.path.expanduser(path.strip() or "~"))
        if not os.path.isdir(target):
            raise FileNotFoundError(target)
        entries: list[DirEntry] = []
        truncated = False
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if entry.name.startswith("."):
                        continue
                    is_link = entry.is_symlink()
                    # Skip symlink directories (same policy as workspace browse).
                    if is_link:
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    entries.append(DirEntry(
                        name=entry.name,
                        path=os.path.join(target, entry.name),
                        is_dir=True,
                        is_symlink=False,
                    ))
                    if len(entries) > max_entries:
                        truncated = True
                        entries = entries[:max_entries]
                        break
                except OSError:
                    continue
        entries.sort(key=lambda e: e.name.casefold())
        return entries, truncated

    def listdir_all(self, path: str, *, max_entries: int = 500) -> tuple[list[DirEntry], bool]:
        """List files and directories (tool use); browse uses listdir dirs-only."""
        target = os.path.realpath(path)
        entries: list[DirEntry] = []
        truncated = False
        with os.scandir(target) as it:
            for entry in it:
                try:
                    is_link = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                    if not (is_dir or is_file):
                        continue
                    entries.append(DirEntry(
                        name=entry.name,
                        path=os.path.join(target, entry.name),
                        is_dir=is_dir,
                        is_symlink=is_link,
                    ))
                    if len(entries) >= max_entries:
                        truncated = True
                        break
                except OSError:
                    continue
        entries.sort(key=lambda e: (not e.is_dir, e.name.casefold()))
        return entries, truncated

    def stat(self, path: str) -> FileStat:
        info = os.lstat(path)
        return FileStat(
            path=path,
            is_dir=stat.S_ISDIR(info.st_mode),
            is_file=stat.S_ISREG(info.st_mode),
            size=int(info.st_size),
            is_symlink=stat.S_ISLNK(info.st_mode),
        )

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"file exceeds max_bytes ({max_bytes})")
        return data

    def write_bytes(self, path: str, data: bytes, *, exclusive: bool = False) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if exclusive:
            flags |= os.O_EXCL
        fd = os.open(path, flags, 0o644)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def close(self) -> None:
        return None


class SftpAdapter:
    """SFTP-backed adapter. Caller owns connection lifecycle via ``close``."""

    def __init__(self, device_id: str, sftp, *, path_style: str = "posix", home: str = "/"):
        self.device_id = device_id
        self._sftp = sftp
        self.path_style = path_style if path_style in {"posix", "windows"} else "posix"
        self.home = home or ("/" if self.path_style == "posix" else "C:\\")

    def confine(self, root: str, candidate: str) -> str:
        return confine_logical(root, candidate, style=self.path_style)

    def parent(self, path: str) -> str | None:
        return parent_path(path, style=self.path_style)

    def _normalize(self, path: str) -> str:
        value = str(path or "").strip() or self.home
        if value.startswith("~"):
            value = join_path(self.home, value[2:] if value.startswith("~/") or value.startswith("~\\") else "", style=self.path_style) if len(value) > 1 else self.home
        return normalize_remote_path(value, style=self.path_style)

    def listdir(self, path: str, *, max_entries: int = 500) -> tuple[list[DirEntry], bool]:
        target = self._normalize(path)
        entries: list[DirEntry] = []
        truncated = False
        for attr in self._sftp.listdir_attr(target):
            name = attr.filename
            if not name or name in {".", ".."} or name.startswith("."):
                continue
            mode = attr.st_mode or 0
            is_link = stat.S_ISLNK(mode)
            is_dir = stat.S_ISDIR(mode)
            if is_link or not is_dir:
                # Browse: directories only; refuse to follow symlinks.
                continue
            child = join_path(target, name, style=self.path_style)
            entries.append(DirEntry(name=name, path=child, is_dir=True, is_symlink=False))
            if len(entries) >= max_entries:
                truncated = True
                break
        entries.sort(key=lambda e: e.name.casefold())
        return entries, truncated

    def listdir_all(self, path: str, *, max_entries: int = 500) -> tuple[list[DirEntry], bool]:
        target = self._normalize(path)
        entries: list[DirEntry] = []
        truncated = False
        for attr in self._sftp.listdir_attr(target):
            name = attr.filename
            if not name or name in {".", ".."}:
                continue
            mode = attr.st_mode or 0
            is_link = stat.S_ISLNK(mode)
            is_dir = stat.S_ISDIR(mode) and not is_link
            is_file = stat.S_ISREG(mode) and not is_link
            if not (is_dir or is_file):
                continue
            child = join_path(target, name, style=self.path_style)
            entries.append(DirEntry(name=name, path=child, is_dir=is_dir, is_symlink=is_link))
            if len(entries) >= max_entries:
                truncated = True
                break
        entries.sort(key=lambda e: (not e.is_dir, e.name.casefold()))
        return entries, truncated

    def stat(self, path: str) -> FileStat:
        target = self._normalize(path)
        # lstat — do not follow symlinks; containment fails closed on links.
        info = self._sftp.lstat(target)
        mode = info.st_mode or 0
        return FileStat(
            path=target,
            is_dir=stat.S_ISDIR(mode),
            is_file=stat.S_ISREG(mode),
            size=int(getattr(info, "st_size", 0) or 0),
            is_symlink=stat.S_ISLNK(mode),
        )

    def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        target = self._normalize(path)
        info = self.stat(target)
        if info.is_symlink:
            raise ValueError("refusing to read symlink")
        if not info.is_file:
            raise ValueError("not a regular file")
        if info.size > max_bytes:
            raise ValueError(f"file exceeds max_bytes ({max_bytes})")
        with self._sftp.open(target, "rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"file exceeds max_bytes ({max_bytes})")
        return data

    def write_bytes(self, path: str, data: bytes, *, exclusive: bool = False) -> None:
        target = self._normalize(path)
        if exclusive:
            try:
                self._sftp.stat(target)
                raise FileExistsError(target)
            except FileNotFoundError:
                pass
            except OSError as exc:
                # Paramiko raises IOError for missing files.
                if getattr(exc, "errno", None) not in {2, None} and "No such file" not in str(exc):
                    # If it exists, fail exclusive create.
                    try:
                        self._sftp.stat(target)
                        raise FileExistsError(target) from exc
                    except FileExistsError:
                        raise
                    except Exception:
                        pass
        with self._sftp.open(target, "wb") as handle:
            handle.write(data)

    def close(self) -> None:
        try:
            self._sftp.close()
        except Exception:
            pass


def adapter_for_device(device, *, sftp=None, path_style: str | None = None) -> FilesystemAdapter:
    if device.transport == "local":
        return LocalAdapter(device.id)
    if device.transport == "ssh":
        if sftp is None:
            raise ValueError("SFTP session required for ssh device")
        style = path_style or "posix"
        return SftpAdapter(device.id, sftp, path_style=style)
    raise ValueError(f"Unsupported transport: {device.transport}")


def infer_remote_path_style(sftp, home: str | None = None) -> str:
    sample = home or "/"
    try:
        sample = sftp.normalize(".")
    except Exception:
        pass
    return detect_path_style(str(sample or "/"))
