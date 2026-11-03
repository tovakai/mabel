"""The python tool sandbox must expose every hop of the interpreter's symlink chain."""
import os

from src.agent_tools import subprocess_tools


def test_symlinked_python_install_dir_is_mounted_unresolved(tmp_path, monkeypatch):
    # uv layout: venv/bin/python -> py/cpython-3.12-linux/bin/python3.12,
    # where cpython-3.12-linux is itself a symlink to cpython-3.12.16-linux.
    real = tmp_path / "py" / "cpython-3.12.16-linux" / "bin"
    real.mkdir(parents=True)
    (real / "python3.12").write_text("")
    link_dir = tmp_path / "py" / "cpython-3.12-linux"
    link_dir.symlink_to(tmp_path / "py" / "cpython-3.12.16-linux")
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(link_dir / "bin" / "python3.12")

    assert subprocess_tools._interpreter_link_dirs(str(venv_bin / "python")) == [str(link_dir)]

    monkeypatch.setattr(subprocess_tools.sys, "executable", str(venv_bin / "python"))
    monkeypatch.setattr(subprocess_tools.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(subprocess_tools.sys, "base_prefix", str(tmp_path / "py" / "cpython-3.12.16-linux"))
    spec = subprocess_tools._owned_spec(str(tmp_path), {"PATH": "/usr/bin"}, 30)
    assert str(link_dir) in spec.readonly_extra                        # mounted at its own path
    assert os.path.realpath(link_dir) in spec.readonly_extra           # and the resolved install
    assert str(tmp_path / "venv") in spec.readonly_extra


def test_plain_interpreter_adds_no_link_dirs(tmp_path):
    binary = tmp_path / "python3"
    binary.write_text("")
    assert subprocess_tools._interpreter_link_dirs(str(binary)) == []


def test_symlink_cycles_terminate(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    assert subprocess_tools._interpreter_link_dirs(str(a)) == []
