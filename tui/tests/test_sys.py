"""SYS (mabel_tui/sysinfo.py, sysview.py): parsers against real output, and the screens by key."""
import asyncio
import dataclasses
from types import SimpleNamespace

from mabel_tui import sysinfo as si

WPCTL = """Audio
 ├─ Devices:
 │      48. GA102 High Definition Audio Controller [alsa]
 │      49. Built-in Audio                      [alsa]
 │
 ├─ Sinks:
 │  *   55. Built-in Audio Analog Stereo        [vol: 0.45]
 │      61. HDMI Out                            [vol: 1.00]
 │
 ├─ Sources:
 │      56. Built-in Audio Analog Stereo        [vol: 1.00]
"""
DECK_BT = """Controller B8:27:EB:26:28:94 (public)
\tName: mabel-deck
\tPowered: no
\tPowerState: off-blocked
\tPairable: no
"""
APLAY = """card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
card 1: vc4hdmi [vc4-hdmi], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
"""


def test_audio_parsers():
    sinks = si.parse_wpctl_sinks(WPCTL)
    assert [(o.id, o.name, o.default) for o in sinks] == [("55", "Built-in Audio Analog Stereo", True),
                                                          ("61", "HDMI Out", False)]
    assert [(c.id, c.name) for c in si.parse_alsa_cards(APLAY)] == [("0", "bcm2835 Headphones"), ("1", "vc4-hdmi")]
    assert si.parse_amixer("  Mono: Playback 0 [96%] [0.00dB] [on]") == (96, False)
    assert si.parse_amixer("  Front Left: Playback 0 [40%] [-12dB] [off]") == (40, True)


def test_bluetooth_and_power_parsers():
    st = si.parse_bt_show(DECK_BT)
    assert st["name"] == "mabel-deck" and not st["powered"] and st["blocked"]
    devs = si.parse_bt_devices("Device AA:BB:CC:DD:EE:FF Keychron K2\nDevice 11:22:33:44:55:66 Buds\n")
    assert devs == [("AA:BB:CC:DD:EE:FF", "Keychron K2"), ("11:22:33:44:55:66", "Buds")]
    d = si.parse_bt_info("AA", "K2", "\tPaired: yes\n\tTrusted: yes\n\tConnected: no\n\tIcon: input-keyboard\n")
    assert (d.paired, d.trusted, d.connected, d.icon) == (True, True, False, "input-keyboard")
    assert si.decode_throttled("throttled=0x50005") == ["under-voltage now", "throttled now",
                                                        "under-voltage since boot", "throttled since boot"]
    assert si.decode_throttled("throttled=0x0") == []


def test_wifi_parsers():
    assert si.split_terse(r"*:Café\:5G:72:WPA2") == ["*", "Café:5G", "72", "WPA2"]
    nets = si.parse_wifi_list("\n".join([" :Home:60:WPA2", "*:Home:80:WPA2", " :Open Cafe:90:", " ::40:WPA2",
                                         " :Neighbour:95:WPA3"]), known={"Home"})
    assert [n.ssid for n in nets] == ["Home", "Neighbour", "Open Cafe"], "active first, then known, then signal"
    assert nets[0].active and nets[0].signal == 80 and nets[0].known and nets[2].security == "open"


def test_sys_screens_by_key(monkeypatch, tmp_path):
    from textual.app import App
    from pathlib import Path
    from mabel_tui import sysview
    from mabel_tui.palette import css_variables
    monkeypatch.setattr(si, "facts", lambda: si.Facts([("host", "zum@test")], ["power: under-voltage now"]))
    monkeypatch.setattr(si, "wifi_radio", lambda: (True, False))
    monkeypatch.setattr(si, "wired", lambda: [("eth0", "ethernet", "connected")])
    calls = []
    monkeypatch.setattr(si, "set_wifi_radio", lambda on: (calls.append(("wifi", on)), (True, "Wi-Fi on"))[1])
    monkeypatch.setattr(si, "bt_state", lambda: {"present": True, "powered": True, "blocked": False, "name": "deck"})
    monkeypatch.setattr(si, "bt_devices", lambda: [si.Device("AA", "Keychron K2", True, True, True, "input-keyboard")])
    monkeypatch.setattr(si, "bt_do", lambda action, mac: (calls.append((action, mac)), (True, "done"))[1])
    monkeypatch.setattr(si, "power", lambda action: (calls.append(("power", action)), (True, action))[1])

    class Host(App):
        CSS_PATH = Path(sysview.__file__).with_name("theme.tcss")

        def __init__(self):
            super().__init__()
            self.cfg = SimpleNamespace(url="http://127.0.0.1:7000", deck=True)

        def get_css_variables(self):
            return {**super().get_css_variables(), **css_variables()}

        def compose(self):
            yield sysview.Sys(True, id="sys")

        def start_nav(self):
            pass

    async def run():
        app = Host()
        async with app.run_test(size=(128, 35)) as pilot:
            suite = app.query_one(sysview.Sys)
            suite.focus_primary()
            await pilot.pause(0.5)
            status = app.query_one("#status", sysview.Page)
            assert "under-voltage" in status.draw(True, 40, 120).plain
            labels = [i.label for i in status.items]
            assert "restart MABEL-OS" in labels and "power off" in labels
            await pilot.press("j", "j", "j", "j", "enter")            # power off: armed, not done
            assert status._armed == "poweroff" and not calls
            suite.show("network")
            await pilot.pause(0.5)
            await pilot.press("enter")                                # Wi-Fi off → on
            await pilot.pause(0.5)
            assert ("wifi", True) in calls
            suite.show("bluetooth")
            await pilot.pause(0.5)
            await pilot.press("j", "j", "enter")                       # the keyboard: asks first
            assert not any(c[0] == "disconnect" for c in calls)
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert ("disconnect", "AA") in calls
    asyncio.run(run())
