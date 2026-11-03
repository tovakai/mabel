"""Owner-scoped, remote-only embedding maintenance for a local Obsidian index.

The source vault and SQLite cache remain on the Arch host. Embedding inference
uses the saved Knowledge embeddings model role, typically BGE-M3 on Dottie.
This module never starts models, writes Markdown, or picks a different owner.
"""
from __future__ import annotations

from src import model_roles


def remote_embedding_assignment(owner: str):
    """Fail closed instead of silently running local FastEmbed on Mabel's CPU."""
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("Embedding maintenance requires the exact Mabel account owner")
    entries = model_roles.candidates("embedding", owner)
    if not entries or not entries[0].model:
        raise model_roles.RoleUnavailable(
            "Configure Settings > AI > Knowledge embeddings for this owner first"
        )
    for assignment in entries:
        if assignment.adapter not in {"openai_embeddings", "ollama_embed"}:
            raise model_roles.CapabilityUnavailable(
                "Automatic vault embeddings require a remote HTTP embedding role; "
                "CPU FastEmbed fallbacks are intentionally disabled"
            )
        if "embedding" not in assignment.capabilities:
            raise model_roles.CapabilityUnavailable(
                "Knowledge embedding role must declare its embedding capability"
            )
        if not assignment.space_id or not assignment.revision or assignment.dimensions < 1:
            raise model_roles.CapabilityUnavailable(
                "Set a stable embedding space, model revision, and dimensions"
            )
        route = model_roles.resolve(assignment, owner, required="embedding")
        if route.url.startswith("local:"):
            raise model_roles.CapabilityUnavailable(
                "Automatic embedding worker cannot use local FastEmbed"
            )
    return entries[0]


async def embed_pending(index, *, owner: str, limit: int = 256, batch_size: int = 8):
    """Process only missing/stale chunk hashes, without rescanning vault files."""
    if not index.status().get("last_scan"):
        raise RuntimeError(
            "Vault index has never been scanned. Start the independent index watcher first."
        )
    primary = remote_embedding_assignment(owner)
    from .semantic import index_embeddings
    result = await index_embeddings(index, owner=owner, limit=limit, batch_size=batch_size)
    return {
        "worker": "remote_embedding",
        "configured_model": primary.model,
        "generated": result["generated"],
        "coverage": result["coverage"],
        "bounded": result["bounded"],
        "routes": result["routes"],
    }
