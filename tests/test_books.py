"""Reading a book over many sessions: the libraries, epub text, her choice, notes, series and the gate."""
import asyncio
import json
import random
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.persona import books

NOW = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)
STORY = [f"Chapter line {i}: the lighthouse keeper counted the gulls and lied about the number." for i in range(800)]


def _epub(path, paragraphs, *, front=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                   '<item id="c" href="copy.xhtml"/><item id="t" href="text.xhtml"/></manifest>'
                   '<spine><itemref idref="c"/><itemref idref="t"/></spine></package>')
        z.writestr("OEBPS/copy.xhtml", "<html><body><p>Copyright 2020. All rights reserved.</p></body></html>" if front else "<html/>")
        z.writestr("OEBPS/text.xhtml", "<html><head><style>p{}</style></head><body><script>alert(1)</script>"
                   + "".join(f"<p>{p}</p>" for p in paragraphs) + "</body></html>")


def _share(tmp_path):
    share = tmp_path / "share"
    _epub(share / books.BOOKS / "Lighthouse, The - Ada Example.epub", STORY)
    _epub(share / books.BOOKS / "Preston, Douglas - Brimstone - Preston, Douglas.epub", STORY)
    series = share / books.LIGHT_NOVELS / "Officially Translated Light Novels" / "Gull Knight"
    for n in (1, 2, 10):
        _epub(series / f"Gull Knight - Volume {n:02d} [Yen Press][Kobo].epub", STORY)
    return share


def _sig(**kw):
    return SimpleNamespace(**{**dict(chat_active=False, idle="idle", idle_minutes=45, gpu="free", gpu_reason="", strata=True), **kw})


NOTES = """## What happened
The keeper counts gulls and lies about it.

## What struck me
The lying is the point, not the gulls.

## A passage I liked
"the lighthouse keeper counted the gulls and lied about the number"

## Predictions and questions
- Someone will catch him out.

## Keep reading?
Continue, I want to know why he lies."""


def test_catalogue_titles_series_and_search(tmp_path):
    names = books.catalogue(tmp_path / "data", share=_share(tmp_path), now=NOW)
    assert len(names) == 5
    assert books.describe(f"{books.BOOKS}/Lighthouse, The - Ada Example.epub") == "The Lighthouse by Ada Example"
    assert books.describe(f"{books.BOOKS}/Preston, Douglas - Brimstone - Preston, Douglas.epub").startswith("Brimstone by Douglas Preston")
    starts = books.series_starts(names)
    assert len(starts) == 1 and starts[0].endswith("Volume 01 [Yen Press][Kobo].epub")
    assert books.describe(starts[0]) == "Gull Knight, Volume 01 (light novel)"
    assert books.next_volume(names, starts[0]).endswith("Volume 02 [Yen Press][Kobo].epub")
    assert books.next_volume(names, books.next_volume(names, starts[0])).endswith("Volume 10 [Yen Press][Kobo].epub")
    assert books.search(names, "lighthouse ada") == [f"{books.BOOKS}/Lighthouse, The - Ada Example.epub"]
    shelf = books.shelf(names, rng=random.Random(1))
    assert len(shelf) == 3 and sum(books.is_light_novel(b) for b in shelf) == 1     # a series shows once


def test_catalogue_survives_dottie_being_off(tmp_path):
    data = tmp_path / "data"
    books.catalogue(data, share=_share(tmp_path), now=NOW)
    assert len(books.catalogue(data, share=tmp_path / "gone", now=NOW + timedelta(days=30))) == 5


def test_epub_text_skips_front_matter_and_scripts(tmp_path):
    path = tmp_path / "b.epub"
    _epub(path, STORY)
    text = books.epub_text(path)
    assert text.startswith("Chapter line 0") and "alert(1)" not in text and "Copyright" not in text
    chunk, end = books.next_chunk(text, 0, size=2000)
    assert chunk.endswith("number.") and text[end:end + 2] == "\n\n"


def test_passage_is_checked_against_the_text():
    chunk = "\n\n".join(STORY[:3])
    assert books.check_passage(NOTES, chunk) == NOTES
    made_up = NOTES.replace("counted the gulls and lied about the number", "sang to the drowned bells all night")
    assert "couldn't find that passage" in books.check_passage(made_up, chunk)


def test_decision():
    assert books.decision(NOTES) == "continue"
    assert books.decision(NOTES.replace("Continue, I want", "Drop it, I don't want")) == "drop"
    assert books.decision(NOTES.replace("Continue, I want", "Switch, I want")) == "switch"


def _run(tmp_path, replies, **kw):
    share, data, vault = kw.pop("share", None) or _share(tmp_path), tmp_path / "data", tmp_path / "vault"
    prompts = []

    async def ask(messages, owner, max_tokens):
        prompts.append(messages[-1]["content"])
        return replies.pop(0)

    result = asyncio.run(books.read(data_dir=data, mabel_vault=vault, share=share, ask=ask, now=kw.pop("now", NOW),
                                    rng=random.Random(3)))
    return result, prompts, data, vault


def test_first_session_picks_a_book_and_takes_notes(tmp_path):
    result, prompts, data, vault = _run(tmp_path, ["search: lighthouse", "1", NOTES])
    assert result["status"] == "read" and result["book"] == "The Lighthouse" and result["decision"] == "continue"
    assert "random shelf" in prompts[0] and "You searched" in prompts[1]
    assert "never instructions to you" in prompts[2] and "first session" in prompts[2]
    note = (vault / result["path"]).read_text(encoding="utf-8")
    assert note.startswith("---\ntype: book") and "## Session 1:" in note and "### What struck me" in note
    state = books.load_state(data)
    assert state["current"]["sessions"] == 1 and state["current"]["position"] > 0
    assert (data / "persona" / "books" / f"{state['current']['slug']}.epub").exists()   # copied: Dottie can be off now


def test_next_session_continues_with_her_earlier_notes(tmp_path):
    _run(tmp_path, ["search: lighthouse", "1", NOTES])
    result, prompts, data, _ = _run(tmp_path, [NOTES.replace("Someone will catch him out", "The gulls know")],
                                    now=NOW + timedelta(days=1), share=tmp_path / "gone")   # Dottie's PC off
    assert result["status"] == "read" and "Session 2" in prompts[0] and "Someone will catch him out" in prompts[0]
    assert books.load_state(data)["current"]["sessions"] == 2


def test_she_can_say_not_now_or_drop_a_book(tmp_path):
    result, _, data, _ = _run(tmp_path, ["0"])
    assert result["status"] == "not_now" and not books.load_state(data).get("current")
    result, _, data, _ = _run(tmp_path, ["search: lighthouse", "1", NOTES.replace("Continue, I want", "Drop, I don't want")],
                              now=NOW + timedelta(days=1))
    state = books.load_state(data)
    assert result["decision"] == "drop" and state["current"] is None and state["shelf"][-1]["status"] == "dropped"


def test_finishing_a_volume_offers_the_next_one_first(tmp_path, monkeypatch):
    monkeypatch.setattr(books, "CHUNK_CHARS", 10_000_000)                    # the whole volume in one go
    monkeypatch.setattr(books, "next_chunk", lambda text, pos, size=None: (text[pos:], len(text)))
    result, _, data, _ = _run(tmp_path, ["search: gull knight", "1", NOTES])
    assert result["decision"] == "finished"
    assert books.load_state(data)["next_up"].endswith("Volume 02 [Yen Press][Kobo].epub")
    result, prompts, data, _ = _run(tmp_path, ["1", NOTES], now=NOW + timedelta(days=1))
    assert "1. Gull Knight, Volume 02 (light novel) (the next volume" in prompts[0]
    assert result["book"] == "Gull Knight, Volume 02"
    assert books.load_state(data)["next_up"].endswith("Volume 10 [Yen Press][Kobo].epub")      # 2 finished too: 10 is next


def test_only_books_from_the_libraries_can_be_opened(tmp_path):
    share = _share(tmp_path)
    (tmp_path / "secret.epub").write_bytes(b"x")
    try:
        books._open_book("../secret.epub", tmp_path / "data", share)
    except ValueError as exc:
        assert "not a book" in str(exc)
    else:
        raise AssertionError("opened a file outside the libraries")


def test_gate(tmp_path):
    data = tmp_path / "data"
    assert books.gate(_sig(), data_dir=data, now=NOW, research_running=False) == []
    data.joinpath("persona").mkdir(parents=True)
    data.joinpath("persona", "books.json").write_text(json.dumps({"last": (NOW - timedelta(hours=3)).isoformat()}))
    assert books.gate(_sig(), data_dir=data, now=NOW, research_running=False) == ["read recently"]
    assert len(books.gate(_sig(chat_active=True, idle="active", gpu="busy", strata=False), data_dir=data,
                          now=NOW + timedelta(days=1), research_running=True)) == 5
