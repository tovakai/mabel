"""Verified metadata migrations stop on proof, not an arbitrary tool limit."""
import json

from src.workspace_edit_completion import verified_markdown_migration_summary

REQUEST = """Examine all Markdown files in the current workspace.
For any file containing the exact line "inventory: [[DVD.base]]", remove that
line and add YAML properties inside its frontmatter:
owned: true
owned_formats: DVD
storage: living room
After making changes, read the files again to verify the results.
"""
BEFORE = "---\ntitle: Test DVD\ninventory: [[DVD.base]]\n---\n"
AFTER = "---\ntitle: Test DVD\nowned: true\nowned_formats: DVD\nstorage: living room\n---\n"
OTHER = "---\ntitle: Already Catalogued\nowned: true\nowned_formats: DVD\nstorage: living room\n---\n"


def evt(tool, path=None, output="", **kwargs):
    cmd = {"path": f"/workspace/{path}"} if path else {}
    cmd.update(kwargs)
    return {"tool": tool, "command": json.dumps(cmd), "output": output, "exit_code": 0}


def trace():
    return [
        evt("ls", output="Already Done.md\nNeeds Update.md"),
        evt("read_file", "Needs Update.md", BEFORE),
        evt("edit_file", "Needs Update.md", "Applied", old_string=BEFORE, new_string=AFTER),
        evt("read_file", "Already Done.md", OTHER),
        evt("read_file", "Needs Update.md", AFTER),
    ]


def test_verified_migration_stops_after_last_read():
    result = verified_markdown_migration_summary(REQUEST, trace())
    assert result is not None
    assert "Needs Update.md" in result and "Already Done.md" in result
    assert "verified" in result.lower()


def test_must_read_changed_file_after_successful_edit():
    assert verified_markdown_migration_summary(REQUEST, trace()[:-1]) is None


def test_all_discovered_files_must_be_inspected():
    events = trace()
    events[0]["output"] += "\nThird.md"
    assert verified_markdown_migration_summary(REQUEST, events) is None


def test_unknown_extra_edit_is_not_authorized_by_task():
    events = trace()
    events[2] = evt("edit_file", "Needs Update.md", "Applied", old_string=BEFORE, new_string=AFTER+"\n## More Details\n")
    assert verified_markdown_migration_summary(REQUEST, events) is None


def test_failed_edits_not_claimed_as_success():
    events = trace()
    events[2]["exit_code"] = 1
    assert verified_markdown_migration_summary(REQUEST, events) is None


def test_unmodified_file_with_old_marker_requires_more_work():
    events = trace()
    events[3]["output"] = BEFORE
    assert verified_markdown_migration_summary(REQUEST, events) is None


def test_unrelated_task_not_short_circuited():
    request = "Edit README.md and run pytest, then deploy the application."
    assert verified_markdown_migration_summary(request, trace()) is None


def test_no_discovery_means_no_exhaustive_completion_claim():
    assert verified_markdown_migration_summary(REQUEST, trace()[1:]) is None


def test_readback_must_show_values_not_just_claim():
    events = trace()
    events[-1]["output"] = "Success!"
    assert verified_markdown_migration_summary(REQUEST, events) is None


def test_readback_must_be_current_after_latest_edit():
    events = trace()
    events.append(evt("edit_file", "Needs Update.md", "Applied", old_string=AFTER, new_string=BEFORE))
    assert verified_markdown_migration_summary(REQUEST, events) is None
