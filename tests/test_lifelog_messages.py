from datetime import datetime, timedelta, timezone

from src.lifelog import home
from src.lifelog.compose import _facts
from src.lifelog.store import Store
from src.lifelog.summary import summarize


def _n(at, package, title, text, **extra):
    return {"entity_id": "sensor.phone_last_notification", "state": text, "last_changed": at,
            "attributes": {"package": package, "android.title": title, "android.text": text, **extra}}


CHANGES = [
    _n("2026-11-05T18:00:00+00:00", "com.whatsapp", "Sam", "are we still on for saturday?"),
    _n("2026-11-05T18:00:00+00:00", "com.whatsapp", "Sam", "are we still on for saturday?"),          # repost
    _n("2026-11-05T18:05:00+00:00", "com.whatsapp", "WhatsApp", "3 new messages from 2 chats"),        # summary
    _n("2026-11-05T18:06:00+00:00", "com.discord", "Kit", "lol look at this",
       **{"android.conversationTitle": "#memes", "android.isGroupConversation": True}),
    _n("2026-11-05T18:07:00+00:00", "com.facebook.orca", "Mum", "call me when you can"),
    _n("2026-11-05T18:08:00+00:00", "com.spotify.music", "Now playing", "a song"),                    # not a chat app
    _n("2026-11-05T18:09:00+00:00", "com.whatsapp", "Work group", "standup moved"),                   # excluded chat
    _n("2026-11-05T18:10:00+00:00", "com.discord", "Discord", "Connected", is_ongoing=True),          # ongoing
]


def test_message_spans_keep_chats_and_drop_noise():
    spans = home.message_spans(CHANGES, exclude_chats=["work group"])
    assert [(s["app"], s["title"]) for s in spans] == [("WhatsApp", "Sam"), ("Discord", "#memes"), ("Messenger", "Mum")]
    discord = spans[1]
    assert discord["detail"] == {"text": "lol look at this", "sender": "Kit", "group": True}
    assert spans[0]["id"] == home.message_spans(CHANGES[:1])[0]["id"]          # stable across polls/restarts
    assert all(s["kind"] == "message" and s["start"] == s["end"] for s in spans)


def test_extra_message_apps_from_settings_mapping():
    spans = home.message_spans([_n("2026-11-05T18:00:00+00:00", "org.telegram.messenger", "Ana", "hi")],
                               apps={**home.MESSAGE_APPS, "org.telegram.messenger": "Telegram"})
    assert spans[0]["app"] == "Telegram"


def test_history_request_asks_for_all_changes_with_attributes():
    seen = {}

    class R:
        status_code = 200
        def json(self):
            return [[CHANGES[0]], [CHANGES[3]]]

    def get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return R()
    now = datetime(2026, 11, 5, 19, tzinfo=timezone.utc)
    out = home._history({"base_url": "http://ha:8123/", "api_key": "k"}, ["sensor.phone_last_notification"],
                        now - timedelta(hours=1), now, get=get)
    assert len(out) == 2 and seen["url"].startswith("http://ha:8123/api/history/period/2026-11-05T18:00")
    assert seen["params"]["significant_changes_only"] == "0" and "minimal_response" not in seen["params"]
    assert home.notification_entities([{"entity_id": "sensor.phone_last_notification"},
                                       {"entity_id": "sensor.phone_steps"}]) == ["sensor.phone_last_notification"]


def test_messages_reach_the_store_summary_and_her_facts(tmp_path):
    store = Store(tmp_path / "lifelog.sqlite")
    now = datetime(2026, 11, 5, 19, tzinfo=timezone.utc)
    spans = home.message_spans(CHANGES + [_n("2026-11-05T18:30:00+00:00", "com.whatsapp", "Sam", "bring snacks")])
    assert store.ingest("phone", spans, source="home_assistant", now=now)["stored"] == 5
    start, end = now - timedelta(hours=12), now + timedelta(hours=12)
    summary = summarize(store.spans(start, end), start=start, end=end)
    sam = summary["messages"][0]
    assert (sam["app"], sam["chat"], sam["count"]) == ("WhatsApp", "Sam", 2)
    assert sam["previews"] == ["are we still on for saturday?", "bring snacks"]
    facts = _facts(summary)
    assert "messages on WhatsApp with/in Sam: 2 incoming" in facts and "“bring snacks”" in facts
    assert "“Kit: lol look at this”" in facts
