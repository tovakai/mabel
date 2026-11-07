import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.persona import news

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>Big thing happens &amp; people react</title><description>&lt;p&gt;Details of the &lt;b&gt;big&lt;/b&gt; thing.&lt;/p&gt;</description>
<link>https://example.com/a</link><pubDate>Sat, 07 Nov 2026 08:00:00 GMT</pubDate></item>
<item><title>Old story</title><description>Stale.</description><link>https://example.com/old</link><pubDate>Sat, 31 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Headline only - Some Paper</title><description>Headline only - Some Paper</description><link>https://news.example/x</link></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom story</title>
<link href="https://example.com/atom"/><updated>2026-11-07T07:00:00Z</updated><summary>Sum.</summary></entry></feed>"""
REPLY = """<think>hm</think># News: Saturday 7 November

The big thing seems overblown to me.

## Things I might look into
- Big things: why do people overreact to them?

## Message to Anthon

Morning. The only thing worth your coffee today is the big thing, and honestly I think people are overreacting.
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "_ledger", lambda *a: None)
    monkeypatch.setattr(news, "OPEN_PAUSE_S", 0)
    sent, prompts = [], []

    async def write(text, opened, owner, label):
        prompts.append((text, opened))
        return REPLY

    def deliver(message, label, journal, owner, day):
        sent.append((message, journal))
        return {"delivery": "sent", "session_id": "s1"}
    data = tmp_path / "data"
    news._save_state(data, {"topics": [{"id": "a", "name": "World", "feed": "https://example.com/rss"}]})
    return SimpleNamespace(data=data, vault=tmp_path / "vault", write=write, deliver=deliver, sent=sent, prompts=prompts)


async def _pick_first(text, owner, label):
    return "1"


def _open(item):
    return f"The whole article about {item['title']}. " * 20


def _read(world, fetch=lambda url: RSS, write=None, now=NOW, deliver=None, pick=_pick_first, open_article=_open):
    return asyncio.run(news.read_news(data_dir=world.data, mabel_vault=world.vault, now=now, fetch=fetch,
                                      write=write or world.write, deliver=deliver or world.deliver, pick=pick,
                                      open_article=open_article))


def test_parse_rss_and_atom_strips_markup():
    items = news.parse_feed(RSS)
    assert items[0]["title"] == "Big thing happens & people react"
    assert items[0]["summary"] == "Details of the big thing." and items[0]["link"] == "https://example.com/a"
    atom = news.parse_feed(ATOM)[0]
    assert atom["link"] == "https://example.com/atom" and atom["summary"] == "Sum." and atom["at"]


def test_entity_declarations_are_refused():
    with pytest.raises(news.NewsError):
        news.parse_feed(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss></rss>')


def test_gather_drops_old_and_seen_stories_and_reports_broken_feeds():
    def fetch(url):
        if "bad" in url:
            raise OSError("down")
        return RSS
    seen = {news._key({"title": "Headline only - Some Paper"})}
    topics = [{"name": "Good", "feed": "https://x/good"}, {"name": "Broken", "feed": "https://x/bad"}]
    sections, failed = news.gather(topics, seen, NOW, fetch)
    assert [i["title"] for i in sections[0]["items"]] == ["Big thing happens & people react"]
    assert failed == ["Broken"]


def test_a_day_of_news_writes_her_journal_and_messages_him_in_prose(world):
    result = _read(world)
    assert result["status"] == "read" and result["delivery"] == "sent"
    note = (world.vault / result["note"]).read_text()
    assert "The big thing seems overblown" in note and "<think>" not in note
    assert "## What I told Anthon" in note and "(https://example.com/a)" in note
    message, journal = world.sent[0]
    assert message.startswith("Morning.") and "#" not in message and "overblown" in journal
    text, opened = world.prompts[0]
    assert "1. Big thing happens" in text and "2. Headline only" in text
    assert opened[0]["title"].startswith("Big thing happens") and "whole article" in opened[0]["text"]
    assert "★ [Big thing happens" in note and "Headline only" in note and "★ [Headline" not in note
    assert result["opened"] == 1


def test_stories_arent_repeated_the_next_day(world):
    _read(world)
    assert _read(world, now=NOW + timedelta(hours=2))["status"] == "nothing_new"
    assert len(world.sent) == 1


def test_a_message_is_required_and_failure_is_recorded_and_retried(world):
    async def no_message(text, opened, owner, label):
        return "# News\n\nJust notes."
    assert _read(world, write=no_message)["status"] == "failed"
    state = news.load_state(world.data)
    assert "without a message" in state["last_error"]["error"] and "last_day" not in state
    assert world.sent == [] and "seen" not in state                  # the stories are still fresh for the retry
    assert _read(world, now=NOW + timedelta(hours=2))["status"] == "read"


def test_all_feeds_down_is_a_failure_not_an_empty_day(world):
    def down(url):
        raise OSError("no network")
    assert _read(world, fetch=down)["status"] == "failed"
    assert "last_day" not in news.load_state(world.data)


def test_topics_can_be_added_and_removed_and_defaults_exist(tmp_path):
    assert {"International", "Sweden", "Anime"} <= {t["name"] for t in news.load_state(tmp_path)["topics"]}
    topic = news.add_topic(tmp_path, "  Steam   Deck ")
    assert topic["name"] == "Steam Deck" and "Steam+Deck" in news.feed_urls(topic)[0]
    with pytest.raises(news.NewsError):
        news.add_topic(tmp_path, "steam deck")
    custom = news.add_topic(tmp_path, "Blog", feed="https://blog.example/rss", check_url=lambda u, block_private: (True, ""))
    assert news.feed_urls(custom) == ["https://blog.example/rss"]
    assert news.remove_topic(tmp_path, topic["id"]) and not news.remove_topic(tmp_path, topic["id"])


def test_questions_join_her_backlog_and_become_a_whisper(world):
    import json
    result = _read(world)
    assert result["questions"] == 1 and result["whisper"] not in ("failed", "no_question")
    backlog = json.loads((world.data / "curiosity" / "backlog.json").read_text())
    assert backlog["questions"][0]["subject"] == "Big things" and backlog["questions"][0]["from_article"] == result["note"]
    whispers = json.loads((world.data / "persona" / "whispers.json").read_text())
    assert "why do people overreact" in whispers["active"]["thought"] and whispers["active"]["source"] == "news"


def test_a_failed_pick_or_article_still_leaves_a_news_day(world):
    async def broken(text, owner, label):
        raise RuntimeError("model hiccup")
    assert _read(world, pick=broken)["status"] == "read" and world.prompts[-1][1] == []

    def wont_open(item):
        raise OSError("paywall")
    world2 = _read(world, now=NOW, open_article=wont_open)
    assert world2["status"] == "nothing_new" or world2["opened"] == 0


def test_picks_are_only_ever_numbers_in_range():
    assert news.parse_picks("<think>3</think>2, 4, 4, 99, 0, ignore this", 5) == [1, 3]
    assert news.parse_picks("1 2 3 4 5 6 7", 9) == [0, 1, 2, 3, 4]
    assert news.parse_picks("nothing today", 5) == []


def test_one_article_that_wont_open_doesnt_stop_the_others(monkeypatch):
    monkeypatch.setattr(news, "OPEN_PAUSE_S", 0)
    items = [{"title": "A", "link": "x", "summary": ""}, {"title": "B", "link": "y", "summary": ""}]

    def opener(item):
        if item["title"] == "A":
            raise OSError("paywall")
        return "text"
    opened = asyncio.run(news.open_articles(items, [0, 1], opener))
    assert [a["title"] for a in opened] == ["B"]


def test_bing_click_links_become_real_article_links():
    wrapped = "http://www.bing.com/news/apiclick.aspx?ref=FexRss&url=https%3a%2f%2fsite.example%2fstory&c=1"
    assert news.real_link(wrapped) == "https://site.example/story"
    assert news.real_link("https://other.example/a") == "https://other.example/a"


def test_each_search_term_gets_its_own_quoted_search():
    urls = news.feed_urls({"name": "Local", "search": 'local LLM OR "llama.cpp"'})
    assert len(urls) == 2 and "%22local+LLM%22" in urls[0] and "%22llama.cpp%22" in urls[1] and "format=rss" in urls[0]
    assert news.feed_urls({"name": "Blog", "feed": "https://b/rss"}) == ["https://b/rss"]


def test_merged_searches_dedupe_and_one_failed_term_is_not_a_failed_topic():
    def fetch(url):
        if "bad" in url:
            raise OSError("down")
        return RSS
    sections, failed = news.gather([{"name": "T", "search": "good OR again OR bad"}], set(), NOW, fetch)
    titles = [i["title"] for i in sections[0]["items"]]
    assert failed == [] and titles.count("Big thing happens & people react") == 1


def test_search_topics_get_a_longer_window_than_feeds():
    old = RSS.replace(b"Sat, 07 Nov 2026 08:00:00", b"Thu, 05 Nov 2026 12:00:00")
    assert "Big thing happens & people react" in [i["title"] for s in news.gather([{"name": "T", "search": "x"}], set(), NOW, lambda u: old)[0] for i in s["items"]]
    assert "Big thing happens & people react" not in [i["title"] for s in news.gather([{"name": "T", "feed": "https://f"}], set(), NOW, lambda u: old)[0] for i in s["items"]]


def test_a_private_feed_address_is_refused(tmp_path):
    with pytest.raises(news.NewsError):
        news.add_topic(tmp_path, "Sneaky", feed="http://127.0.0.1:7000/rss")


def test_no_message_goes_out_while_reaching_out_is_paused(monkeypatch):
    monkeypatch.setattr(news, "_setting", lambda k, d: True if k == "initiative_paused" else d)
    assert news._deliver("hi", "Sat", "j", "o", "2026-11-07") == {"delivery": "journal_only"}


def _sig(**kw):
    return SimpleNamespace(**{**dict(chat_active=False, idle="idle", idle_minutes=30, gpu="free", gpu_reason="", strata=True), **kw})


def test_gate_is_once_a_day_after_the_hour_when_shes_free(world, monkeypatch):
    from src import gpu_swap
    monkeypatch.setattr(news, "_setting", lambda k, d: 0 if k == "news_hour" else d)
    monkeypatch.setattr(gpu_swap, "active", lambda: None)
    g = lambda now=NOW, **kw: news.gate(_sig(**kw), data_dir=world.data, now=now, research_running=False)
    assert g() == []
    assert g(chat_active=True) and g(idle="active") and g(gpu="busy", gpu_reason="game") and g(strata=False)
    _read(world)
    assert "already read today's news" in g(now=NOW + timedelta(hours=3))
    monkeypatch.setattr(news, "_setting", lambda k, d: 25 if k == "news_hour" else d)
    state = news.load_state(world.data); state.pop("last_day"); state.pop("last_attempt"); news._save_state(world.data, state)
    assert "too early" in g()


def test_the_message_prompt_talks_to_him_not_about_him():
    import inspect
    source = inspect.getsource(news._write)
    assert 'sent straight to Anthon' in source and 'talking to him as "you"' in source
    assert "What you'd actually tell Anthon" not in source


def test_news_message_prompt_explains_before_reacting():
    import inspect
    assert "say what actually happened and who's involved before you react" in inspect.getsource(news._write)


def test_research_messages_teach_first_with_the_facts_in_hand():
    import inspect
    from src.initiative import outreach
    described = outreach._describe(1, {"source": "research", "title": "T", "found": "OLM animates Pokémon.", "take": "Mine."})
    assert "What you found: OLM animates Pokémon." in described
    assert "teach first" in inspect.getsource(outreach.review) and outreach.MESSAGE_LIMIT >= 900
