"""One-shot semantic *suggestions* between source-verified promoted episodes.

No persistent vectors, graph edges, memory writes, chat prompt injection,
or role fallback. A consented operator selects the already-configured,
owner-scoped Knowledge Embeddings role (normally Dottie's BGE-M3).
Numerical similarity is model output, NOT proof of a factual relationship.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .associations import _verified_node
from .candidates import CandidateAdmissionError, CandidateQueue, CapturePolicy
from .promotion import _ID
from .recall import _search_root


def _embedding_route(owner: str):
    """Resolve only the explicitly selected, owner-scoped embedding endpoint."""
    from src import model_roles
    from src.knowledge_engine.semantic import space

    assignment = model_roles.primary_assignment("embedding", owner=owner)
    if (
        not assignment.endpoint_id or not assignment.model
        or assignment.adapter not in {"openai_embeddings", "ollama_embed"}
        or "embedding" not in assignment.capabilities
    ):
        raise CandidateAdmissionError(
            "Configure an explicit owner-scoped remote Knowledge Embeddings endpoint "
            "with a supported adapter and embedding capability"
        )
    try:
        embedding_space = space(assignment)
        route = model_roles.resolve(assignment, owner=owner, required="embedding")
    except (model_roles.RoleUnavailable, ValueError) as exc:
        raise CandidateAdmissionError(
            "The configured owner-scoped Knowledge Embeddings model is unavailable"
        ) from exc
    return route, embedding_space


def _embedding_text(node: dict) -> str:
    # Never send interpretation/model-authored claims, file paths, owner names,
    # arbitrary Markdown, or entire transcripts to the remote endpoint.
    # _verified_node uses _verified_transcript's 500-char excerpt bounds.
    return (
        "User excerpt: " + node["user_excerpt"][:500]
        + "\nAssistant excerpt: " + node["assistant_excerpt"][:500]
    )


def _snapshot(node: dict) -> tuple:
    return (
        node["candidate_id"], node["source_ref"], node["user_excerpt"],
        node["assistant_excerpt"], node["note_path"],
    )


async def suggest_semantic_episode_links(
    queue: CandidateQueue,
    seed_candidate_id: str,
    *,
    db,
    vault_root: str | Path,
    protected_roots: tuple[str | Path, ...],
    policy: CapturePolicy,
    approved: bool = False,
    send_source_to_embedding: bool = False,
    scan_limit: int = 8,
    max_suggestions: int = 5,
    min_similarity: float = 0.70,
    after_candidate_id: str | None = None,
    embed_call=None,
    wait_for_quiet=None,
) -> dict:
    """Compute bounded advisory cosine similarities with no persistence.

    The input is a verified seed and a deterministic page of promoted,
    independently verified episodes. All excerpts and permissions are
    rechecked after inference. This call never supplies model advice to
    a chat prompt and never modifies a candidate or Markdown note.
    """
    if approved is not True or send_source_to_embedding is not True:
        raise PermissionError(
            "Explicit approval to read and send each source excerpt to the configured embedding role is required"
        )
    if not isinstance(queue, CandidateQueue) or not isinstance(policy, CapturePolicy):
        raise TypeError("Trusted owner-scoped queue and CapturePolicy required")
    policy.require_capture()
    if not isinstance(seed_candidate_id, str) or not _ID.fullmatch(seed_candidate_id):
        raise CandidateAdmissionError("Invalid semantic seed candidate ID")
    if after_candidate_id is not None and (
        not isinstance(after_candidate_id, str) or not _ID.fullmatch(after_candidate_id)
    ):
        raise CandidateAdmissionError("Invalid semantic pagination cursor")
    if type(scan_limit) is not int or not 1 <= scan_limit <= 16:
        raise CandidateAdmissionError("Semantic scan limit must be 1..16")
    if type(max_suggestions) is not int or not 1 <= max_suggestions <= 8:
        raise CandidateAdmissionError("Semantic suggestion limit must be 1..8")
    if type(min_similarity) not in (float, int) or not 0 <= min_similarity <= 1:
        raise CandidateAdmissionError("Semantic similarity threshold must be 0..1")

    root = _search_root(queue, vault_root, protected_roots)
    seed_row = queue.get(seed_candidate_id)
    if seed_row is None or seed_row["state"] != "promoted":
        raise CandidateAdmissionError("Seed is not a promoted episode")
    seed = _verified_node(queue, seed_row, db, root)
    if seed is None:
        raise CandidateAdmissionError("Seed does not have current verified source evidence")

    with queue._connect() as local:
        fetched = [dict(row) for row in local.execute(
            "SELECT * FROM candidates WHERE owner=? AND state='promoted' "
            "AND id>? ORDER BY id LIMIT ?",
            (queue.owner, after_candidate_id or "", scan_limit + 1),
        )]
    page = fetched[:scan_limit]
    cursor = page[-1]["id"] if len(fetched) > scan_limit and page else None
    valid = []
    skipped = 0
    for row in page:
        if row["id"] == seed_candidate_id:
            continue
        node = _verified_node(queue, row, db, root)
        if node is None:
            skipped += 1
            continue
        valid.append(node)

    if not valid:
        return {
            "owner": queue.owner, "seed_candidate_id": seed_candidate_id,
            "suggestions": [], "scanned": len(page), "skipped_invalid": skipped,
            "next_after_candidate_id": cursor,
            "mode": "explicit_semantic_advisory", "embedding_invoked": False,
        }

    route, embedding_space = _embedding_route(queue.owner)
    if embed_call is None:
        from src.knowledge_engine.semantic import embed
        embed_call = embed
    if wait_for_quiet is None:
        from src.interactive_gate import wait_for_interactive_quiet
        wait_for_quiet = wait_for_interactive_quiet
    try:
        await asyncio.wait_for(
            wait_for_quiet("Dottie episodic semantic association"), timeout=90,
        )
    except asyncio.TimeoutError as exc:
        raise CandidateAdmissionError("Foreground work remained active; semantic scan deferred") from exc

    # Never submit interpretation or candidate summaries as semantic truth.
    payload = [_embedding_text(node) for node in [seed, *valid]]
    raw = await asyncio.wait_for(embed_call(route, payload), timeout=60)

    # Normalize and reject wrong shape, NaN, zero and incompatible dimensions
    # even when a test adapter or future alternative embedder is supplied.
    from src.knowledge_engine.semantic import vectors
    normalized = vectors(raw, len(payload), route.assignment.dimensions)
    policy.require_capture()
    refreshed_seed = _verified_node(queue, seed_row, db, root)
    if refreshed_seed is None or _snapshot(refreshed_seed) != _snapshot(seed):
        raise CandidateAdmissionError("Seed source was modified or revoked during semantic inference")

    scored = []
    for previous, vector in zip(valid, normalized[1:]):
        current = _verified_node(queue, queue.get(previous["candidate_id"]) or {}, db, root) if queue.get(previous["candidate_id"]) else None
        if current is None or _snapshot(current) != _snapshot(previous):
            skipped += 1
            continue
        # Both normalized vectors are unit-length. Cosine may be negative,
        # but only nonnegative values above operator threshold are suggested.
        similarity = sum(a * b for a, b in zip(normalized[0], vector))
        if similarity < min_similarity:
            continue
        scored.append({
            "source_id": seed_candidate_id,
            "target_id": current["candidate_id"],
            "relation_type": "semantic_similarity_suggestion",
            "cosine_similarity": round(min(1.0, max(-1.0, similarity)), 4),
            "embedding_space": embedding_space,
            "evidence_kind": "inference_over_verified_transcript_excerpts",
            "source_ref": current["source_ref"],
            "note_path": current["note_path"],
            "user_excerpt": current["user_excerpt"],
            "assistant_excerpt": current["assistant_excerpt"],
        })
    scored.sort(key=lambda row: (-row["cosine_similarity"], row["target_id"]))
    return {
        "owner": queue.owner,
        "seed_candidate_id": seed_candidate_id,
        "suggestions": scored[:max_suggestions],
        "scanned": len(page),
        "skipped_invalid": skipped,
        "next_after_candidate_id": cursor,
        "truncated_suggestions": len(scored) > max_suggestions,
        "mode": "explicit_semantic_advisory",
        "embedding_invoked": True,
    }
