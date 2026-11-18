"""VAULT: the Obsidian model (links, backlinks, tags, rename, daily notes) and the vim editor."""
import asyncio
from datetime import date

from mabel_tui.vault import Vault, fill_template, moment
from mabel_tui.vaultview import obsidian_to_markdown


def make(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "daily-notes.json").write_text('{"folder": "daily", "template": "tpl"}')
    (tmp_path / "tpl.md").write_text("<< [[{{yesterday}}]] >>\n# {{date:dddd, MMMM Do, YYYY}}\n")
    (tmp_path / "Library").mkdir()
    (tmp_path / "Library" / "Monster.md").write_text("---\ntags: [anime, seinen]\n---\n# Monster\n## Plot\nUrasawa.\n")
    (tmp_path / "Watchlist.md").write_text("Loved [[Monster]] and [[Library/Monster|the manga]] #favourite\n"
                                           "```\n[[NotALink]] #nottag\n```\n")
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "old.md").write_text("[[Monster]]")
    return Vault(tmp_path).scan()


def test_links_backlinks_tags_and_headings(tmp_path):
    v = make(tmp_path)
    assert set(v.notes) == {"tpl.md", "Library/Monster.md", "Watchlist.md"}            # hidden folders skipped
    monster, watch = v.notes["Library/Monster.md"], v.notes["Watchlist.md"]
    assert v.resolve("Monster", watch) is monster and v.resolve("Library/Monster", watch) is monster
    assert [n.rel for n in v.backlinks(monster)] == ["Watchlist.md"]
    assert monster.tags == {"anime", "seinen"} and watch.tags == {"favourite"}           # not inside code
    assert [(l, t) for l, t, _ in monster.headings] == [(1, "Monster"), (2, "Plot")]
    assert "NotALink" not in watch.links


def test_rename_rewrites_every_link_form(tmp_path):
    v = make(tmp_path)
    note, changed = v.rename(v.notes["Library/Monster.md"], "Monster (manga)")
    text = (tmp_path / "Watchlist.md").read_text()
    assert changed == 1 and "[[Monster (manga)]]" in text and "[[Library/Monster (manga)|the manga]]" in text
    assert [n.rel for n in v.backlinks(note)] == ["Watchlist.md"]


def test_daily_note_comes_from_the_template(tmp_path):
    v = make(tmp_path)
    note = v.daily(date(2026, 11, 18))
    assert note.rel == "daily/2026-11-18.md"
    assert note.path.read_text() == "<< [[2026-11-17]] >>\n# Wednesday, November 18th, 2026\n"
    assert v.daily(date(2026, 11, 18)) is v.notes["daily/2026-11-18.md"]                 # not recreated
    assert moment(date(2026, 1, 2), "Do MMM YY [week] w") == "2nd Jan 26 week 1"


def test_obsidian_extras_become_clickable_markdown():
    md = obsidian_to_markdown("---\nstatus: done\n---\n> [!tip]+ Remember\nSee [[Monster#Plot|plot]] #anime ==hi==\n")
    assert "**status**: done" in md and "**✦ Remember**" in md
    assert "[plot](wiki:Monster#Plot)" in md and "`#anime`" in md and "**hi**" in md


def test_vim_keys_edit_like_vim():
    from textual.app import App
    from mabel_tui.vimarea import VimArea

    class Host(App):
        def compose(self):
            yield VimArea("alpha beta\ngamma\n")

    async def run():
        async with Host().run_test() as pilot:
            area = pilot.app.query_one(VimArea)
            area.focus()
            for key in ["w", "d", "w"]:                     # delete "beta"
                await pilot.press(key)
            assert area.text.split("\n")[0].rstrip() == "alpha"
            for key in ["j", "y", "y", "p"]:                # duplicate "gamma"
                await pilot.press(key)
            assert area.text.split("\n")[1:3] == ["gamma", "gamma"]
            await pilot.press("u")                          # undo the paste
            assert area.text.split("\n")[1:3] != ["gamma", "gamma"]
            await pilot.press("i"); await pilot.press("X"); await pilot.press("escape")
            assert area.mode == "normal" and "X" in area.text
    asyncio.run(run())
