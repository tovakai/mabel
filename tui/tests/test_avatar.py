"""The pixel avatar: states from avatar.json, Aseprite timings, exact 1×2 px cells, talk gating."""
import asyncio
import json

from PIL import Image

from mabel_tui.avatar import DEFAULT, Avatar, load_animations, load_frames


def test_the_built_in_avatar_has_idle_and_talk():
    anims = load_animations(None)
    assert set(anims) >= {"idle", "talk"}
    for a in anims.values():
        assert len(a.frames) == 11 and len(a.frames[0]) == 28 and len(a.frames[0][0]) == 28
    assert anims["talk"].ms[-1] == 33                                   # the blink frames are quick
    assert (DEFAULT / "still.png").exists()


def test_idle_holds_its_first_frame_so_blinks_come_irregularly():
    idle = load_animations(None)["idle"]
    holds = {round(idle.duration(0), 2) for _ in range(20)}
    assert min(holds) >= 2.0 and max(holds) <= 5.0 and len(holds) > 1
    assert idle.duration(1) == 0.1 and idle.duration(9) == 0.033


def test_a_folder_without_idle_falls_back_and_a_single_png_is_a_still(tmp_path):
    strip = Image.new("RGBA", (28 * 3, 28), (0, 0, 0, 0))
    strip.putpixel((28, 0), (255, 0, 0, 255))
    strip.save(tmp_path / "talk.png")
    Image.new("RGBA", (28, 28), (9, 9, 9, 255)).save(tmp_path / "still.png")
    (tmp_path / "avatar.json").write_text(json.dumps({"talk": {"file": "talk.png", "ms": [50]}}))
    anims = load_animations(tmp_path)
    assert len(anims["talk"].frames) == 3 and anims["talk"].frames[1][0][0] == (255, 0, 0)
    assert anims["idle"].frames[0][0][0] == (9, 9, 9)
    assert set(load_animations(tmp_path / "still.png")) == {"idle"}


def test_cells_are_top_and_bottom_pixels(tmp_path):
    im = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    im.putpixel((0, 0), (255, 0, 0, 255)); im.putpixel((0, 1), (0, 0, 255, 255)); im.putpixel((1, 1), (0, 255, 0, 255))
    im.save(tmp_path / "t.png")
    segs = list(Avatar(tmp_path / "t.png").render_line(0))
    assert segs[0].text == "▀" and segs[0].style.color.name == "#ff0000" and segs[0].style.bgcolor.name == "#0000ff"
    assert segs[1].text == "▄" and segs[1].style.color.name == "#00ff00"


def test_she_talks_only_while_text_arrives():
    """A fake reply: 1.2 s of thinking, text for ~1 s, a 1.5 s tool pause, more text."""
    from mabel_tui import config
    from mabel_tui.app import MabelApp
    from mabel_tui.comms import Comms

    async def fake_stream(session_id, text, approval=None):
        yield {"type": "turn_mode"}
        await asyncio.sleep(1.2)
        for word in "Oi you nerd, ".split(" ") * 3:
            yield {"delta": word + " "}
            await asyncio.sleep(0.1)
        yield {"type": "tool_start", "tool": "search"}
        await asyncio.sleep(1.5)
        for word in "grab the cola".split():
            yield {"delta": word + " "}
            await asyncio.sleep(0.1)
        yield {"type": "final_response", "content": "Oi you nerd, grab the cola"}

    async def run():
        app = MabelApp(config.load())
        async with app.run_test(size=(170, 48)) as pilot:
            await pilot.pause(4)                                     # the real chat finishes opening first
            comms = app.query_one(Comms)
            comms.session = {"id": "x"}
            app.mabel.stream = fake_stream
            avatar = app.query_one("Avatar")
            seen = []
            worker = comms.run_turn("hi")
            for _ in range(60):                                      # sample every 0.1 s
                await asyncio.sleep(0.1)
                seen.append(avatar.state)
            await worker.wait()
            return seen
    seen = asyncio.run(run())
    runs = []                                                        # [state, samples] in order
    for state in seen:
        if runs and runs[-1][0] == state:
            runs[-1][1] += 1
        else:
            runs.append([state, 1])
    assert [r[0] for r in runs] == ["idle", "talk", "idle", "talk", "idle"], runs
    assert runs[0][1] >= 10                                          # thinking: no mouth before the text
    assert runs[2][1] >= 5                                           # quiet through the tool call
