"""The pixel avatar: a still, a strip of frames, transparency, exact 1×2 px cells."""
from PIL import Image

from mabel_tui.avatar import DEFAULT, Avatar, load_frames


def test_the_built_in_avatar_is_28_by_28():
    frames = load_frames(DEFAULT)
    assert len(frames) == 1 and len(frames[0]) == 28 and len(frames[0][0]) == 28


def test_a_strip_of_square_frames_animates(tmp_path):
    strip = Image.new("RGBA", (28 * 3, 28), (0, 0, 0, 0))
    strip.putpixel((28, 0), (255, 0, 0, 255))                    # frame 2, top-left
    strip.save(tmp_path / "anim.png")
    frames = load_frames(tmp_path / "anim.png")
    assert len(frames) == 3
    assert frames[0][0][0] is None and frames[1][0][0] == (255, 0, 0)


def test_cells_are_top_and_bottom_pixels(tmp_path):
    im = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    im.putpixel((0, 0), (255, 0, 0, 255)); im.putpixel((0, 1), (0, 0, 255, 255)); im.putpixel((1, 1), (0, 255, 0, 255))
    im.save(tmp_path / "t.png")
    a = Avatar(tmp_path / "t.png")
    segs = list(a.render_line(0))
    assert segs[0].text == "▀" and segs[0].style.color.name == "#ff0000" and segs[0].style.bgcolor.name == "#0000ff"
    assert segs[1].text == "▄" and segs[1].style.color.name == "#00ff00"     # transparent top shows the panel
