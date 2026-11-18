"""TERM: a real shell in a pty, its keys, its colours, and pyte quirks."""
import asyncio

from mabel_tui.term import Terminal, _Screen, ansi_colours
import pyte


def test_private_sgr_sequences_are_ignored_not_fatal():
    screen = _Screen(40, 5, history=10)
    stream = pyte.ByteStream(screen)
    stream.feed(b"\x1b[>4;2m\x1b[31mred\x1b[0m")             # vim's modifyOtherKeys request, then colour
    assert "".join(screen.buffer[0][x].data for x in range(3)) == "red"
    assert screen.buffer[0][0].fg == "red"


def test_terminal_colours_follow_the_palette():
    colours = ansi_colours()
    from mabel_tui.palette import P
    assert colours["brown"] == P.amber and colours["cyan"] == P.teal and colours["black"] == P.bg


def test_a_shell_runs_and_answers_keys():
    from textual.app import App

    class Host(App):
        def compose(self):
            yield Terminal()

    def text(t):
        return "\n".join("".join(t.screen_.buffer[y][x].data for x in range(t.screen_.columns))
                         for y in range(t.screen_.lines))

    async def run():
        async with Host().run_test(size=(80, 20)) as pilot:
            term = pilot.app.query_one(Terminal)
            term.focus()
            await pilot.pause(1)
            for ch in "echo mabel$((40+2))":
                await pilot.press({" ": "space", "$": "dollar_sign", "(": "left_parenthesis",
                                   ")": "right_parenthesis", "+": "plus"}.get(ch, ch))
            await pilot.press("enter")
            await pilot.pause(1)
            assert "mabel42" in text(term)
            term.close()
    asyncio.run(run())
