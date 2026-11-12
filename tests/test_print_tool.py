"""print_on_paper: "Mabel, can you print…" reaches the receipt printer; code talk about print() doesn't."""
import asyncio
import json

import pytest

from src.agent_tools.printer_tool import PrintTool
from src.persona import printer
from src.turn_contract import _REQUIRED_TOOLS, requested_capabilities


def _run(tool, args):
    return asyncio.run(tool.execute(json.dumps(args), {}))


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(printer, "enabled", lambda: True)
    return tmp_path


def test_prints_right_away_and_keeps_a_note(vault):
    sent = []
    tool = PrintTool(mabel_vault=vault, data_dir=vault, sender=lambda data: sent.append(data) or "bluetooth")
    out = _run(tool, {"text": "milk\neggs\ncoffee", "title": "Shopping"})
    result = json.loads(out["output"])
    assert out["exit_code"] == 0 and result["status"].startswith("printed")
    assert b"Shopping" in sent[0] and b"coffee" in sent[0]
    note = (vault / result["note"]).read_text()
    assert "asked_by: Anthon" in note and "milk" in note
    assert printer.load_state(vault)["done"][-1]["kind"] == "chat"


def test_waits_for_the_printer_when_it_is_off(vault):
    def off(data):
        raise OSError("bluetooth: [Errno 112] Host is down")
    out = _run(PrintTool(mabel_vault=vault, data_dir=vault, sender=off), {"text": "a note"})
    assert json.loads(out["output"])["status"].startswith("queued: the printer didn't answer")
    assert len(printer.load_state(vault)["pending"]) == 1          # the heartbeat prints it once it's on


@pytest.mark.parametrize("args", [{"text": ""}, {"text": "x" * 5000}])
def test_refuses_empty_or_too_long(vault, args):
    out = _run(PrintTool(mabel_vault=vault, data_dir=vault, sender=lambda d: "usb"), args)
    assert out["exit_code"] == 1 and not printer.load_state(vault)["pending"]


@pytest.mark.parametrize("message", [
    "Mabel, can you print my shopping list: milk, eggs",
    "Mabel print the recipe",
    "Could you print out that poem?",
    "Hey Mabel, print me a haiku",
    "print this",
])
def test_print_requests_require_the_printer(message):
    assert requested_capabilities(message) == {"printer"}
    assert _REQUIRED_TOOLS["printer"] == "print_on_paper"


@pytest.mark.parametrize("message", [
    "Print the output of ls",
    "print hello world in python",
    "How do I print in python?",
    "print(x)",
    "Can you print the logs?",
    "I printed it yesterday",
    "The printer is out of paper",
])
def test_code_and_chatter_about_printing_dont(message):
    assert "printer" not in requested_capabilities(message)
