"""Explicit, source-verified Phase 00.5 promotion to Mabel's own vault.

No chat hook, automatic promotion, embedding, or recall. The caller must supply
a trusted owner-scoped candidate queue, an enabled capture policy, an actual
database session, an explicitly selected vault, and human authorization.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from sqlalchemy import text

from .candidates import CandidateAdmissionError, CandidateQueue, CapturePolicy, _now
from .capture import _excerpt

_SOURCE = re.compile(
    r"chat:(?P<session>[A-Za-z0-9_.:-]{1,128}):message:"
    r"(?P<message>[A-Za-z0-9_.:-]{1,128})"
)
_ID = re.compile(r"[0-9a-f]{32}")


def _verified_transcript(queue: CandidateQueue, candidate: dict, db) -> tuple[str, str]:
    """Prove the *transcript* exists; never certify claims within model prose."""
    if candidate["source_kind"] != "conversation" or candidate["kind"] != "experience":
        raise CandidateAdmissionError("Only persisted conversation experiences can be promoted")
    match = _SOURCE.fullmatch(candidate["source_ref"])
    if not match or candidate["source_event_id"] != "message:" + match["message"]:
        raise CandidateAdmissionError("Candidate has an inconsistent persisted source identity")
    session = db.execute(
        text("SELECT owner, memory_extraction_enabled FROM sessions WHERE id=:id"),
        {"id": match["session"]},
    ).mappings().first()
    # No fallback to legacy/shared sessions or null owners.
    from src.owner_identity import owner_key
    if not session or owner_key(session["owner"]) != queue.owner:
        raise CandidateAdmissionError("Persisted source session is missing or belongs to another owner")
    if session["memory_extraction_enabled"] is False or session["memory_extraction_enabled"] == 0:
        raise PermissionError("Source session has memory extraction disabled")
    assistant = db.execute(
        text("SELECT id, role, content, timestamp FROM chat_messages "
             "WHERE id=:id AND session_id=:session"),
        {"id": match["message"], "session": match["session"]},
    ).mappings().first()
    if not assistant or assistant["role"] != "assistant" or assistant["timestamp"] is None:
        raise CandidateAdmissionError("Persisted assistant receipt is missing")
    user = db.execute(
        text("SELECT content FROM chat_messages WHERE session_id=:session AND role='user' "
             "AND (timestamp < :at OR (timestamp = :at AND id < :id)) "
             "ORDER BY timestamp DESC, id DESC LIMIT 1"),
        {"session": match["session"], "at": assistant["timestamp"], "id": assistant["id"]},
    ).mappings().first()
    if not user:
        raise CandidateAdmissionError("Matching persisted user turn was not found")
    user_excerpt = _excerpt(user["content"], limit=500)
    assistant_excerpt = _excerpt(assistant["content"], limit=500)
    original = f"User said: {user_excerpt}\nMabel replied: {assistant_excerpt}"
    if candidate["summary"] != original:
        raise CandidateAdmissionError("Stored candidate no longer matches the source transcript")
    if len(user_excerpt) < 16 or len(assistant_excerpt) < 24:
        raise CandidateAdmissionError("Source transcript is not substantive")
    return user_excerpt, assistant_excerpt


def _fenced(value: str) -> str:
    """Treat transcript text as data, not executable Obsidian Markdown."""
    # A source can contain ordinary fences; choose a longer delimiter.
    width = max(4, max((len(m.group(0)) for m in re.finditer(r"~+", value)), default=0) + 1)
    fence = "~" * width
    return f"{fence}text\n{value}\n{fence}\n"


def _episode_markdown(candidate: dict, user: str, assistant: str) -> str:
    ident = candidate["id"]
    return (
        "---\n"
        f"id: episode_{ident}\n"
        "type: episodic\n"
        "actor: mabel\n"
        f"owner: {json.dumps(candidate['owner'])}\n"
        f"source_kind: {json.dumps(candidate['source_kind'])}\n"
        f"source_ref: {json.dumps(candidate['source_ref'])}\n"
        f"source_event_id: {json.dumps(candidate['source_event_id'])}\n"
        f"occurred_at: {json.dumps(candidate['created_at'])}\n"
        "evidence_status: transcript_verified\n"
        "status: active\n"
        "---\n\n"
        "# A persisted conversation\n\n"
        "This note verifies that these words appear in the stored conversation. "
        "It does **not** verify the truth of statements made by either party.\n\n"
        "## User message (source excerpt)\n\n" + _fenced(user) + "\n"
        "## Mabel reply (source excerpt)\n\n" + _fenced(assistant) + "\n"
        "## Interpretation (unverified, not factual evidence)\n\n"
        + (_fenced(candidate["interpretation"]) if candidate["interpretation"].strip()
           else "No interpretation recorded.\n")
    )


# Mabel's vault keeps private knowledge in lowercase top-level folders that
# Syncthing replicates and Git ignores (see .gitignore / .stignore). Episodes
# live under memory/, never a differently-cased sibling Git would track.
EPISODE_DIR = ("memory", "episodes")


def _require_git_ignored(root: Path, target: Path) -> None:
    """In a Git checkout, refuse to write unless Git ignores the episode path.

    The vault root may double as a public source repository; a private
    conversation excerpt must never become committable.
    """
    if not (root / ".git").exists():
        return
    import subprocess
    try:
        checked = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--",
                                  str(target.relative_to(root))], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise CandidateAdmissionError("Cannot confirm Git ignores the episode path") from error
    if checked.returncode != 0:
        raise CandidateAdmissionError("Episode path is not Git-ignored in this repository; refusing to write")


def _target(queue: CandidateQueue, candidate_id: str, vault_root: str | Path,
            protected_roots: tuple[str | Path, ...]) -> Path:
    if not _ID.fullmatch(candidate_id):
        raise CandidateAdmissionError("Invalid candidate identifier")
    if not protected_roots:
        raise CandidateAdmissionError("Explicit protected source vault roots required")
    root = Path(vault_root).expanduser()
    if not root.exists() or not root.is_dir():
        raise CandidateAdmissionError("Configured Mabel vault must already exist")
    root = root.resolve()
    for protected in protected_roots:
        protected_path = Path(protected).expanduser().resolve()
        if root == protected_path or root.is_relative_to(protected_path):
            raise CandidateAdmissionError("Cannot write episodic memory inside a protected source vault")
    if queue.path.resolve().is_relative_to(root / EPISODE_DIR[0]):
        raise CandidateAdmissionError("SQLite candidate queue must remain outside the synced memory folder")
    owner_bucket = sha256(queue.owner.encode("utf-8")).hexdigest()[:24]
    _require_git_ignored(root, root.joinpath(*EPISODE_DIR, owner_bucket, f"{candidate_id}.md"))
    parent = root
    for piece in (*EPISODE_DIR, owner_bucket):
        parent = parent / piece
        if parent.is_symlink():
            raise CandidateAdmissionError("Refusing symlinked episode directory")
        parent.mkdir(mode=0o700, exist_ok=True)
        if not parent.is_dir() or not parent.resolve().is_relative_to(root):
            raise CandidateAdmissionError("Episode destination escapes the Mabel vault")
    return parent / f"{candidate_id}.md"


def _write_once(path: Path, content: bytes) -> None:
    """Atomic create, never overwrite; compare identical replay bytes."""
    if path.is_symlink():
        raise CandidateAdmissionError("Episode destination is a symlink")
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise CandidateAdmissionError("Existing episode differs from verified source")
        return
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".episode-", suffix=".tmp",
                                         delete=False) as stream:
            temp_name = stream.name
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Hard-linking an already fsynced temp file is atomic and exclusive.
            # A crash before the SQLite commit leaves a complete, replayable note.
            os.link(temp_name, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise CandidateAdmissionError("Conflicting concurrent episode file")
        if hasattr(os, "O_DIRECTORY"):
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def _promote(
    queue: CandidateQueue,
    candidate_id: str,
    *,
    vault_root: str | Path,
    protected_roots: tuple[str | Path, ...],
    policy: CapturePolicy,
    approved: bool,
    build,
    evidence: str,
) -> dict:
    """Shared promotion core: ``build(row)`` verifies the source and returns the note text.

    Stage a stable digest in the local queue before creating the Markdown file.
    If the process crashes after creation but before finalization, a retry with
    the same source finds the exact bytes and safely completes. The event
    outbox and promoted state are committed together, for later Phase 00 flush.
    """
    if approved is not True:
        raise PermissionError("Explicit operator approval is required")
    if not isinstance(queue, CandidateQueue) or not isinstance(policy, CapturePolicy):
        raise TypeError("Trusted candidate queue and capture policy required")
    policy.require_capture()
    row = queue.get(candidate_id)
    if row is None:
        raise CandidateAdmissionError("Candidate not found for owner")
    if row["state"] not in ("pending", "deferred", "promoting", "promoted"):
        raise CandidateAdmissionError("Rejected or quarantined candidates are not promotable")
    if row["state"] == "promoted":
        path = _target(queue, candidate_id, vault_root, protected_roots)
        if (path.is_symlink() or not path.is_file()
                or sha256(path.read_bytes()).hexdigest() != row["decision_reason"]):
            raise CandidateAdmissionError("Promoted episode missing or changed")
        return {**row, "changed": False, "episode_path": str(path)}

    body = build(row).encode("utf-8")
    digest = sha256(body).hexdigest()
    path = _target(queue, candidate_id, vault_root, protected_roots)
    with queue._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM candidates WHERE owner=? AND id=?",
            (queue.owner, candidate_id),
        ).fetchone()
        if current is None:
            raise CandidateAdmissionError("Candidate disappeared")
        if current["state"] == "promoted":
            # Another worker completed the same promotion while we verified.
            if not path.is_file() or path.is_symlink() or path.read_bytes() != body:
                raise CandidateAdmissionError("Concurrent promotion produced different bytes")
            return {**dict(current), "changed": False, "episode_path": str(path)}
        if current["state"] == "promoting":
            if current["decision_reason"] != digest:
                raise CandidateAdmissionError("In-progress promotion source changed; manual recovery required")
        elif current["state"] in ("pending", "deferred"):
            conn.execute(
                "UPDATE candidates SET state='promoting', decision_reason=?, updated_at=? "
                "WHERE owner=? AND id=?",
                (digest, _now(), queue.owner, candidate_id),
            )
        else:
            raise CandidateAdmissionError("Rejected or quarantined candidates are not promotable")

    _write_once(path, body)

    with queue._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM candidates WHERE owner=? AND id=?",
            (queue.owner, candidate_id),
        ).fetchone()
        if current["state"] == "promoted":
            return {**dict(current), "changed": False, "episode_path": str(path)}
        if current["state"] != "promoting" or current["decision_reason"] != digest:
            raise CandidateAdmissionError("Promotion intent changed unexpectedly")
        conn.execute(
            "UPDATE candidates SET state='promoted', evidence_status=?, "
            "updated_at=? WHERE owner=? AND id=?",
            (evidence, _now(), queue.owner, candidate_id),
        )
        conn.execute(
            "INSERT INTO event_outbox(id,candidate_id,kind,payload,created_at) "
            "VALUES(?,?,?,?,?)",
            queue._envelope(
                candidate_id, "memory.candidate_promoted",
                {"decision": "promoted", "source_kind": current["source_kind"],
                 "source_ref": current["source_ref"], "reason_code": "operator_approved"},
            ),
        )
        final = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        return {**dict(final), "changed": True, "episode_path": str(path)}


def promote_verified_conversation(
    queue: CandidateQueue,
    candidate_id: str,
    *,
    db,
    vault_root: str | Path,
    protected_roots: tuple[str | Path, ...],
    policy: CapturePolicy,
    approved: bool = False,
) -> dict:
    """Explicit operator-reviewed promotion of a persisted conversation turn."""
    return _promote(queue, candidate_id, vault_root=vault_root, protected_roots=protected_roots, policy=policy,
                    approved=approved, evidence="transcript_verified",
                    build=lambda row: _episode_markdown(row, *_verified_transcript(queue, row, db)))


# --- moments from Mabel's daily account of Anthon's day (src/lifelog) ------------------------

_DAY_SOURCE = re.compile(r"lifelog:day:(?P<date>\d{4}-\d{2}-\d{2})")
DAY_DIR = ("journal", "anthon")
# Moments from her correspondence with Tova verify against the note she wrote on reading a letter.
_LETTER_SOURCE = re.compile(r"letter:note:(?P<note>(?P<date>\d{4}-\d{2}-\d{2})(?:-\d+)?)")
LETTER_NOTE_DIR = ("journal", "letters", "notes")
NOTE_SOURCE_PREFIXES = ("lifelog:day:", "letter:note:")
MOMENTS_HEADING = "Moments worth remembering"


def day_moments(text: str) -> list[str]:
    """The bullets under "Moments worth remembering" in a day note."""
    match = re.search(rf"^##\s+{re.escape(MOMENTS_HEADING)}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not match:
        return []
    return [" ".join(re.sub(r"^[-*\d.)\s]+", "", line).split())
            for line in match.group(1).splitlines() if line.strip().startswith(("-", "*"))]


def _verified_day_moment(candidate: dict, vault_root: str | Path) -> tuple[str, str]:
    """Prove the moment still appears in Mabel's day note; returns (date, moment)."""
    ref = str(candidate.get("source_ref") or "")
    day, letter = _DAY_SOURCE.fullmatch(ref), _LETTER_SOURCE.fullmatch(ref)
    match = day or letter
    if candidate.get("source_kind") != "tool_observation" or candidate.get("kind") != "experience" or not match:
        raise CandidateAdmissionError("Not a moment from a daily account")
    name = f"{letter.group('note')}.md" if letter else f"{match.group('date')}.md"
    path = Path(vault_root).expanduser().joinpath(*(LETTER_NOTE_DIR if letter else DAY_DIR), name)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise CandidateAdmissionError("The daily account this moment came from is missing")
    moment = " ".join(str(candidate.get("summary") or "").split())
    if moment not in day_moments(path.read_text(encoding="utf-8")):
        raise CandidateAdmissionError("The daily account no longer contains this moment")
    return match.group("date"), moment


def _day_moment_markdown(candidate: dict, date: str, moment: str) -> str:
    letter = _LETTER_SOURCE.fullmatch(str(candidate.get("source_ref") or ""))
    title = f"A moment from her letters with Tova ({date})" if letter else f"A moment from Anthon's day ({date})"
    basis = (f"This note verifies that the moment appears in what Mabel wrote on reading a letter "
             f"(journal/letters/notes/{letter.group('note')}.md). It is her own recollection of a correspondence; "
             "anything Tova said in it is Tova's word, not established fact.\n\n") if letter else (
        "This note verifies that the moment appears in Mabel's own account of that day "
        f"(journal/anthon/{date}.md), which she wrote from recorded activity and his journal. "
        "It is her recollection, not independent proof that it happened exactly so.\n\n")
    return (
        "---\n"
        f"id: episode_{candidate['id']}\n"
        "type: episodic\n"
        "actor: mabel\n"
        f"owner: {json.dumps(candidate['owner'])}\n"
        f"source_kind: {json.dumps(candidate['source_kind'])}\n"
        f"source_ref: {json.dumps(candidate['source_ref'])}\n"
        f"source_event_id: {json.dumps(candidate['source_event_id'])}\n"
        f"occurred_at: {json.dumps(date)}\n"
        "evidence_status: day_note_verified\n"
        "status: active\n"
        "---\n\n"
        f"# {title}\n\n"
        + basis +
        "## Moment\n\n" + _fenced(moment)
    )


def promote_verified_day_moment(
    queue: CandidateQueue,
    candidate_id: str,
    *,
    vault_root: str | Path,
    protected_roots: tuple[str | Path, ...],
    policy: CapturePolicy,
    approved: bool = False,
) -> dict:
    """Explicit operator-reviewed promotion of a moment from a daily account."""
    return _promote(queue, candidate_id, vault_root=vault_root, protected_roots=protected_roots, policy=policy,
                    approved=approved, evidence="day_note_verified",
                    build=lambda row: _day_moment_markdown(row, *_verified_day_moment(row, vault_root)))


def promote_verified(queue: CandidateQueue, candidate_id: str, *, db, vault_root, protected_roots,
                     policy: CapturePolicy, approved: bool = False) -> dict:
    """Promote whichever kind of candidate this is (conversation turn or day moment)."""
    row = queue.get(candidate_id)
    if row is not None and str(row.get("source_ref") or "").startswith(NOTE_SOURCE_PREFIXES):
        return promote_verified_day_moment(queue, candidate_id, vault_root=vault_root,
                                           protected_roots=protected_roots, policy=policy, approved=approved)
    return promote_verified_conversation(queue, candidate_id, db=db, vault_root=vault_root,
                                         protected_roots=protected_roots, policy=policy, approved=approved)
