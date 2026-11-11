"""Pasted material and ALL-CAPS headings are not requests to audit vault files."""
from src.agent_runtime.structured_audits import audit_requirement


def test_a_pasted_sheet_with_a_field_notes_heading_is_not_an_audit():
    rows = "\n".join(f"{i:08d} Item {i} | box {i % 7} | blue/lavender bin | Checked out? no" for i in range(60))
    paste = "Total reset. Index everything.\n\nTHE SORTING INDEX / FIELD NOTES\n\nLOGGED 60 REVIEW 3\n" + rows
    assert audit_requirement(paste) is None


def test_caps_heading_alone_is_not_a_property_name():
    assert audit_requirement("Check THE SORTING INDEX / FIELD NOTES in my notes") is None


def test_real_requests_still_are_audits():
    assert audit_requirement("Audit the owned property across my Library/Movies notes").property == "owned"
    assert audit_requirement("How many notes in Library/Books have the field called read set?").property == "read"
