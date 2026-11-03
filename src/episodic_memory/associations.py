"""Read-only, source-grounded associations between promoted episodic memories.

Links are ephemeral observations about *source evidence*: two verified
conversation excerpts have words in common or occurred in the same saved
session. They are not assertions about the truth or causality of those
experiences. No relationship is written to SQLite or Markdown, and no model
receives these excerpts or injects them into chat.
"""
from __future__ import annotations

from pathlib import Path

from .candidates import CandidateAdmissionError, CandidateQueue, CapturePolicy
from .dottie_review import _check_source_memory_controls
from .promotion import _ID, _SOURCE, _verified_transcript
from .recall import _search_root, _tokens, _verified_episode_path

# Source-conversation boilerplate is not evidence of a relationship. This
# list is intentionally conservative and applies only to lexical similarity,
# never to the exact same-session relationship.
_COMMON = frozenset({
    "about", "after", "again", "also", "always", "another", "because",
    "before", "being", "between", "both", "could", "does", "doing",
    "during", "each", "every", "from", "have", "into", "just",
    "make", "made", "many", "more", "most", "much", "need",
    "only", "other", "over", "really", "said", "same", "should",
    "some", "still", "such", "than", "that", "their", "them",
    "there", "these", "they", "thing", "things", "this", "those",
    "through", "time", "today", "tomorrow", "under", "using",
    "very", "want", "were", "what", "when", "where", "which",
    "while", "will", "with", "would", "your", "ours", "ourselves",
    "user", "users", "assistant", "mabel", "reply", "replied",
    "message", "messages", "conversation", "conversations", "episode",
    "episodes", "source", "sources", "notes", "note", "text",
    "test", "tests", "testing", "confirmed", "found", "discussed",
    "talked", "explained", "remember", "remembered", "remembering",
    "something", "everything", "nothing", "following", "previous",
})


def _topic_words(text: str) -> set[str]:
    return {
        token for token in _tokens(text)
        if len(token) >= 4 and token not in _COMMON
    }


def _verified_node(queue, candidate, db, bucket: Path) -> dict | None:
    """Read a still-promoted source with the same gates as manual recall."""
    if (
        candidate["state"] != "promoted"
        or candidate["kind"] != "experience"
        or candidate["source_kind"] != "conversation"
        or _SOURCE.fullmatch(candidate["source_ref"]) is None
    ):
        return None
    path = _verified_episode_path(
        bucket, candidate["id"], candidate["decision_reason"]
    )
    if path is None:
        return None
    try:
        user, assistant = _verified_transcript(queue, candidate, db)
        _check_source_memory_controls(db, candidate)
    except (CandidateAdmissionError, PermissionError):
        return None

    # Recheck after I/O for a concurrent revocation/retention cleanup.
    current = queue.get(candidate["id"])
    if (
        current is None
        or current["state"] != "promoted"
        or current["decision_reason"] != candidate["decision_reason"]
        or current["source_ref"] != candidate["source_ref"]
    ):
        return None

    source = _SOURCE.fullmatch(candidate["source_ref"])
    return {
        "candidate_id": candidate["id"],
        "source_ref": candidate["source_ref"],
        "source_session": source["session"],
        "note_path": path,
        "occurred_at": candidate["created_at"],
        "evidence_status": "transcript_verified",
        "user_excerpt": user,
        "assistant_excerpt": assistant,
        "_terms": _topic_words(user + " " + assistant),
        "_user_terms": _topic_words(user),
    }


def _connection(first: dict, second: dict) -> dict | None:
    """Compute typed, inspectable evidence for a connection, never a claim."""
    if first["candidate_id"] == second["candidate_id"]:
        return None
    same_session = first["source_session"] == second["source_session"]
    common = first["_terms"].intersection(second["_terms"])
    # Cross-session single-word resemblance is too weak to be called a
    # relation. Two concrete terms, or the same persisted session, qualify.
    if not same_session and len(common) < 2:
        return None
    user_common = first["_user_terms"].intersection(second["_user_terms"])
    types = []
    if same_session:
        types.append("same_source_session")
    if len(common) >= 2:
        types.append("shared_source_terms")
    score = 3 * int(same_session) + min(16, len(common) * 2) + min(8, len(user_common))
    return {
        "source_id": first["candidate_id"],
        "target_id": second["candidate_id"],
        "relation_types": types,
        "shared_terms": sorted(common)[:8],
        "association_score": score,
        "evidence_kind": "observed_transcript_relationship",
    }


def _public_node(node: dict, *, distance: int) -> dict:
    return {
        "candidate_id": node["candidate_id"],
        "source_ref": node["source_ref"],
        "note_path": node["note_path"],
        "occurred_at": node["occurred_at"],
        "evidence_status": node["evidence_status"],
        "user_excerpt": node["user_excerpt"],
        "assistant_excerpt": node["assistant_excerpt"],
        "distance": distance,
    }


def related_verified_episodes(
    queue: CandidateQueue,
    seed_candidate_id: str,
    *,
    db,
    vault_root: str | Path,
    protected_roots: tuple[str | Path, ...],
    policy: CapturePolicy,
    approved: bool = False,
    max_depth: int = 2,
    max_nodes: int = 20,
    max_edges: int = 40,
    scan_limit: int = 200,
    after_candidate_id: str | None = None,
) -> dict:
    """Explore up to two evidence-linked hops from an approved verified seed.

    The graph is reconstructed for each operator call from currently promoted
    candidate rows and canonical source receipts. Relations are not persisted.
    Traversal is bounded by row scan, nodes, edges and depth; any pagination
    cursor pertains to the candidate *scan*, not a stable persistent graph.
    """
    if approved is not True:
        raise PermissionError("Explicit read consent required for associations")
    if not isinstance(queue, CandidateQueue) or not isinstance(policy, CapturePolicy):
        raise TypeError("Trusted owner-scoped queue and policy are required")
    policy.require_capture()
    if not isinstance(seed_candidate_id, str) or not _ID.fullmatch(seed_candidate_id):
        raise CandidateAdmissionError("Invalid association seed ID")
    if type(max_depth) is not int or not 1 <= max_depth <= 2:
        raise CandidateAdmissionError("Association depth must be 1 or 2")
    if type(max_nodes) is not int or not 2 <= max_nodes <= 30:
        raise CandidateAdmissionError("Association node limit must be 2..30")
    if type(max_edges) is not int or not 1 <= max_edges <= 100:
        raise CandidateAdmissionError("Association edge limit must be 1..100")
    if type(scan_limit) is not int or not 1 <= scan_limit <= 500:
        raise CandidateAdmissionError("Association scan limit must be 1..500")
    if after_candidate_id is not None and (
        not isinstance(after_candidate_id, str) or not _ID.fullmatch(after_candidate_id)
    ):
        raise CandidateAdmissionError("Invalid association scan cursor")

    bucket = _search_root(queue, vault_root, protected_roots)
    seed_row = queue.get(seed_candidate_id)
    if seed_row is None or seed_row["state"] != "promoted":
        raise CandidateAdmissionError("Association seed is not a promoted episode")
    seed = _verified_node(queue, seed_row, db, bucket)
    if seed is None:
        raise CandidateAdmissionError("Association seed has no current verified evidence")

    with queue._connect() as local:
        rows = [dict(row) for row in local.execute(
            "SELECT * FROM candidates WHERE owner=? AND state='promoted' "
            "AND id>? ORDER BY id LIMIT ?",
            (queue.owner, after_candidate_id or "", scan_limit + 1),
        )]
    page = rows[:scan_limit]
    next_after = page[-1]["id"] if len(rows) > scan_limit and page else None

    validated = {seed_candidate_id: seed}
    skipped = 0
    for row in page:
        if row["id"] == seed_candidate_id:
            continue
        node = _verified_node(queue, row, db, bucket)
        if node is None:
            skipped += 1
            continue
        validated[node["candidate_id"]] = node

    # Bounded breadth-first expansion. Calculate edges only for visited
    # frontiers, avoiding a quadratic persisted/ever-growing graph structure.
    ids = [seed_candidate_id]
    distances = {seed_candidate_id: 0}
    edges = []
    seen_edges = set()
    truncated = False
    for depth in range(max_depth):
        frontier = [node_id for node_id in ids if distances[node_id] == depth]
        for node_id in frontier:
            candidates = []
            for other_id, other in validated.items():
                if other_id == node_id:
                    continue
                edge = _connection(validated[node_id], other)
                if edge is not None:
                    candidates.append((edge, other_id))
            candidates.sort(key=lambda pair: (
                -pair[0]["association_score"], pair[1],
            ))
            for edge, other_id in candidates:
                if other_id not in distances:
                    if len(ids) >= max_nodes:
                        truncated = True
                        continue
                    ids.append(other_id)
                    distances[other_id] = depth + 1
                pair = tuple(sorted((node_id, other_id)))
                if pair in seen_edges:
                    continue
                if len(edges) >= max_edges:
                    truncated = True
                    continue
                seen_edges.add(pair)
                edges.append(edge)

    return {
        "owner": queue.owner,
        "seed_candidate_id": seed_candidate_id,
        "nodes": [_public_node(validated[i], distance=distances[i]) for i in ids],
        "edges": edges,
        "scanned": len(page),
        "skipped_invalid": skipped,
        "verified_candidates": len(validated),
        "omitted_unconnected": len(validated) - len(ids),
        "next_after_candidate_id": next_after,
        "truncated": truncated,
        "mode": "explicit_read_only_associations",
    }
