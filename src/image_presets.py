"""Per-model prompt presets, the token budget and tag search for image generation.

Some image models need fixed tags to work well (Pony's score tags, Pixel Party's trigger
phrase). A preset holds them per model and they're applied to every request, whoever wrote
the prompt. ``data/image_presets.json`` overrides or extends ``DEFAULTS`` by model id:

    {"my-model": {"positive": "tag, tag", "suffix": ". in some style", "negative": "...", "tags": true}}

``positive`` goes in front of the prompt, ``suffix`` at the end, ``negative`` is sent as the
negative prompt, and ``tags`` marks models that take booru tags, for autocomplete.

Tag search reads ``data/image_tags.csv`` (tag,category,count,aliases), if it's there.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_LIMIT = 77                     # CLIP, start and end tokens included
MAX_TAGS = 8
DEFAULTS = {
    "pony-diffusion-v6-xl": {
        "positive": "score_9, score_8_up, score_7_up, score_6_up, score_5_up, score_4_up",
        "negative": "score_6, score_5, score_4, worst quality, low quality, bad anatomy, bad hands, text, "
                    "watermark, blurry, jpeg artifacts, extra limbs",
        "tags": True},
    "pixel-party-xl": {"suffix": ". in pixel art style", "negative": "mixels. amateur. multiple"},
    "cyberrealistic-xl": {"negative": "cartoon, illustration, anime, painting, CGI, 3D render, low quality, "
                                      "watermark, logo, label"},
}
_counter = None
_tags: list[tuple[str, int, str]] | None = None


def _data_dir() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR)


def preset_for(model: str) -> dict:
    presets = dict(DEFAULTS)
    try:
        extra = json.loads((_data_dir() / "image_presets.json").read_text(encoding="utf-8"))
        if isinstance(extra, dict):
            presets.update({str(k): v for k, v in extra.items() if isinstance(v, dict)})
    except (OSError, ValueError):
        pass
    return dict(presets.get(str(model or ""), {}))


def apply(model: str, prompt: str) -> tuple[str, str]:
    """(full prompt, negative prompt) for this model. Tags already in the prompt aren't doubled."""
    preset = preset_for(model)
    text = str(prompt or "").strip()
    suffix = str(preset.get("suffix") or "").strip()
    if suffix and suffix.lower().lstrip(". ") not in text.lower():
        text = f"{text.rstrip(' .')}{suffix if suffix.startswith(('.', ',')) else ' ' + suffix}"
    positive = str(preset.get("positive") or "").strip().rstrip(",")
    if positive and not text.lower().startswith(positive.lower()):
        text = f"{positive}, {text}"
    return text, str(preset.get("negative") or "").strip()


def tokens(model: str, prompt: str, count=None) -> dict:
    """The CLIP budget for this prompt on this model, preset included."""
    global _counter
    if count is None:
        if _counter is None:
            from src.persona.dreams import token_counter
            _counter = token_counter()
        count = _counter
    used = count(apply(model, prompt)[0])
    return {"used": used, "limit": TOKEN_LIMIT, "left": TOKEN_LIMIT - used}


def _load_tags() -> list[tuple[str, int, str]]:
    global _tags
    if _tags is None:
        rows = []
        try:
            with open(_data_dir() / "image_tags.csv", encoding="utf-8", newline="") as handle:
                for row in csv.reader(handle):
                    if len(row) >= 3 and row[2].isdigit():
                        rows.append((row[0], int(row[2]), row[3].lower() if len(row) > 3 else ""))
        except OSError:
            logger.info("[images] no tag list at data/image_tags.csv")
        rows.sort(key=lambda r: -r[1])
        _tags = rows
    return _tags


def display(tag: str) -> str:
    """Prompts use spaces; underscores stay in short emoticons like ^_^."""
    return tag.replace("_", " ") if re.search(r"[A-Za-z0-9]_[A-Za-z0-9]", tag) else tag


def search(query: str, limit: int = MAX_TAGS) -> list[dict]:
    """Tags starting with ``query`` first (most used first), then aliases."""
    q = str(query or "").strip().lower().replace(" ", "_")
    if len(q) < 2:
        return []
    limit = min(int(limit), 20)
    starts, aliased = [], []
    for tag, posts, aliases in _load_tags():
        if tag.startswith(q):
            starts.append({"tag": display(tag), "posts": posts})
            if len(starts) >= limit:
                break
        elif len(aliased) < limit and q in aliases and any(a.startswith(q) for a in aliases.split(",")):
            aliased.append({"tag": display(tag), "posts": posts})
    return (starts + aliased)[:limit]
