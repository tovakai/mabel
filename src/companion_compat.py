"""Small request compatibility helpers for third-party chat clients.

Keep compatibility policy separate from the large streaming route so a plugin
upgrade cannot silently change the semantics of optional form fields.
"""


def form_flag_enabled(value: object) -> bool:
    """Treat only explicit true values as enabled (never Python string truthiness)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def include_agent_skills_index(mode: str, no_skills: object = None) -> bool:
    """Whether to inject the skills index into *this request's context*.

    This does NOT grant or revoke tool permissions. Agent tool execution is
    still governed by the server's existing permission/approval policy.
    """
    return mode == "agent" and not form_flag_enabled(no_skills)
