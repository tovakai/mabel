#!/usr/bin/env python3
"""Install user-systemd units for independent Obsidian indexing and Dottie embeddings.

Run from the checkout that should power the services. No systemctl commands
are invoked by this installer; review generated units before enabling them.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import shutil
import sys
from datetime import datetime, timezone


def _unit_path(value: Path) -> str:
    # systemd interprets percent specifiers in unit files.
    return str(value.resolve()).replace("%", "%%")


def render(*, checkout: Path, python: Path, env_file: Path, every_minutes: int,
           interval: float, reconcile_seconds: int, embedding_limit: int,
           embedding_batch_size: int) -> dict[str, str]:
    root = _unit_path(checkout)
    command = shlex.quote(_unit_path(python))
    environment = _unit_path(env_file)
    common = (
        "[Unit]\nDescription=Mabel Obsidian knowledge index maintenance\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\n"
        f"WorkingDirectory={root}\nEnvironmentFile={environment}\n"
        "UMask=0077\nNice=10\nIOSchedulingClass=idle\n"
    )
    vault_and_owner = "--vault ${MABEL_KNOWLEDGE_VAULT} --owner ${MABEL_KNOWLEDGE_OWNER}"
    index_service = (
        common +
        "Type=simple\n"
        f"ExecStart={command} -m src.knowledge_engine {vault_and_owner} "
        f"watch --interval {interval:g} --reconcile-seconds {reconcile_seconds}\n"
        "Restart=on-failure\nRestartSec=20s\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    embed_service = (
        common.replace("Description=Mabel Obsidian knowledge index maintenance",
                       "Description=Mabel Dottie remote embedding maintenance") +
        "Type=oneshot\n"
        f"ExecStart={command} -m src.knowledge_engine {vault_and_owner} "
        f"embed --embedding-limit {embedding_limit} --batch-size {embedding_batch_size}\n"
        "TimeoutStartSec=15min\n"
    )
    embed_timer = (
        "[Unit]\nDescription=Periodically embed changed Mabel vault chunks via Dottie\n\n"
        "[Timer]\nOnBootSec=90s\n"
        f"OnUnitInactiveSec={every_minutes}min\nAccuracySec=15s\n"
        "Unit=mabel-knowledge-embed.service\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return {
        "mabel-knowledge-index.service": index_service,
        "mabel-knowledge-embed.service": embed_service,
        "mabel-knowledge-embed.timer": embed_timer,
    }


def install(*, checkout: Path, python: Path, vault: Path, owner: str,
            systemd_dir: Path, env_file: Path, every_minutes: int = 2,
            interval: float = 2, reconcile_seconds: int = 120,
            embedding_limit: int = 256, embedding_batch_size: int = 8,
            replace: bool = False) -> list[Path]:
    checkout, python, vault = checkout.resolve(), python.resolve(), vault.resolve()
    if not (checkout / "src/knowledge_engine/__main__.py").is_file():
        raise ValueError("Checkout must contain src/knowledge_engine/__main__.py")
    if not python.is_file():
        raise ValueError("Pass an existing Mabel virtualenv Python interpreter")
    if not vault.is_dir():
        raise ValueError("Vault directory is missing")
    if not owner or any(c in owner for c in "\r\n\0"):
        raise ValueError("Set the exact Mabel session owner")
    if every_minutes < 1 or interval < 0.5 or reconcile_seconds < interval:
        raise ValueError("Invalid service polling interval")
    if not 1 <= embedding_limit <= 10000 or not 1 <= embedding_batch_size <= 64:
        raise ValueError("Invalid embedding budget")
    contents = render(checkout=checkout, python=python, env_file=env_file,
                      every_minutes=every_minutes, interval=interval,
                      reconcile_seconds=reconcile_seconds, embedding_limit=embedding_limit,
                      embedding_batch_size=embedding_batch_size)
    contents["knowledge-index.env"] = (
        "MABEL_KNOWLEDGE_VAULT=" + json.dumps(str(vault), ensure_ascii=False) + "\n"
        "MABEL_KNOWLEDGE_OWNER=" + json.dumps(owner, ensure_ascii=False) + "\n"
    )
    targets = {systemd_dir / name: content for name, content in contents.items()
               if name != "knowledge-index.env"}
    targets[env_file] = contents["knowledge-index.env"]
    existing = [p for p in targets if p.exists()]
    if existing and not replace:
        raise FileExistsError(
            "Refusing to overwrite existing service configuration: " +
            ", ".join(str(p) for p in existing) + " (use --replace to back it up)"
        )
    systemd_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if existing:
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for p in existing:
            shutil.copy2(p, p.with_name(p.name + ".bak-" + suffix))
    for p, value in targets.items():
        # Unit content contains no secret token; env file still private by default.
        temp = p.with_name(p.name + ".tmp")
        temp.write_text(value, encoding="utf-8")
        temp.chmod(0o600 if p == env_file else 0o644)
        temp.replace(p)
    return list(targets)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vault", type=Path, required=True)
    p.add_argument("--owner", required=True, help="Exact owner of Mabel's saved embedding role")
    p.add_argument("--checkout", type=Path, default=Path.cwd())
    p.add_argument("--python", type=Path, default=Path(sys.executable))
    p.add_argument("--every-minutes", type=int, default=2)
    p.add_argument("--interval", type=float, default=2)
    p.add_argument("--reconcile-seconds", type=int, default=120)
    p.add_argument("--embedding-limit", type=int, default=256)
    p.add_argument("--embedding-batch-size", type=int, default=8)
    p.add_argument("--install", action="store_true", help="Write files; does not enable/start services")
    p.add_argument("--replace", action="store_true", help="Back up existing unit files before replacing")
    args = p.parse_args(argv)
    home = Path.home()
    systemd_dir = home / ".config/systemd/user"
    env_file = home / ".config/mabel/knowledge-index.env"
    params = dict(checkout=args.checkout, python=args.python, vault=args.vault,
                  owner=args.owner, systemd_dir=systemd_dir, env_file=env_file,
                  every_minutes=args.every_minutes, interval=args.interval,
                  reconcile_seconds=args.reconcile_seconds,
                  embedding_limit=args.embedding_limit,
                  embedding_batch_size=args.embedding_batch_size)
    if args.install:
        for path in install(**params, replace=args.replace):
            print("Installed:", path)
        print("Next: systemctl --user daemon-reload")
        print("Then: systemctl --user enable --now mabel-knowledge-index.service mabel-knowledge-embed.timer")
    else:
        validated_checkout = args.checkout.resolve()
        if not (validated_checkout / "src/knowledge_engine/__main__.py").is_file():
            p.error("Run from a Mabel checkout or pass --checkout")
        generated = render(checkout=validated_checkout, python=args.python,
                           env_file=env_file, every_minutes=args.every_minutes,
                           interval=args.interval, reconcile_seconds=args.reconcile_seconds,
                           embedding_limit=args.embedding_limit,
                           embedding_batch_size=args.embedding_batch_size)
        for name, unit in generated.items():
            print(f"--- {name} ---\n{unit}")
        print(f"Not installed. With --install, vault={args.vault.resolve()} owner={args.owner}")


if __name__ == "__main__":
    main()
