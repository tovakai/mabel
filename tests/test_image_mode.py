from routes.chat_routes import _image_mode_from_request


def test_image_mode_defaults_to_auto():
    assert _image_mode_from_request({}, None) == "auto"
    assert _image_mode_from_request({"image_mode": "banana"}, None) == "auto"


def test_image_mode_reads_form_then_json_body():
    assert _image_mode_from_request({"image_mode": "ON"}, None) == "on"
    assert _image_mode_from_request({}, {"image_mode": "off"}) == "off"
    assert _image_mode_from_request({"image_mode": "off"}, {"image_mode": "on"}) == "off"


def test_chat_route_wires_image_mode():
    import inspect
    import routes.chat_routes as cr
    src = inspect.getsource(cr)
    # off: both image tools disabled for the turn; on: the turn becomes a direct image turn.
    assert 'disabled_tools.update({"generate_image", "edit_image"})' in src
    assert "image_generation_session = True" in src
    assert 'json.dumps({"prompt": _user_msg, "size": "1024x1024", "model": image_model_choice})' in src


def test_image_model_choice_is_validated():
    from routes.chat_routes import _image_model_from_request
    assert _image_model_from_request({"image_model": "pony-diffusion-v6-xl"}, None) == "pony-diffusion-v6-xl"
    assert _image_model_from_request({}, {"image_model": "pixel-party-xl"}) == "pixel-party-xl"
    assert _image_model_from_request({"image_model": "bad name; rm -rf"}, None) == ""
    assert _image_model_from_request({}, None) == ""


def test_menu_choice_overrides_the_default_image_model_for_this_request_only(monkeypatch):
    import asyncio
    import contextvars
    from src import ai_interaction, settings
    monkeypatch.setattr(settings, "load_settings", lambda: {"image_gen_enabled": True, "image_model": "pixel-party-xl"})
    asked = []

    def resolve(spec, owner=None, model_type=None):
        asked.append(spec)
        raise ValueError("stop here")
    monkeypatch.setattr(ai_interaction, "_resolve_model", resolve)

    async def with_choice():
        ai_interaction.IMAGE_MODEL_OVERRIDE.set("pony-diffusion-v6-xl")
        return await ai_interaction.do_generate_image('{"prompt": "a cat"}')
    contextvars.Context().run(asyncio.run, with_choice())
    asyncio.run(ai_interaction.do_generate_image('{"prompt": "a dream"}'))   # e.g. dreams: no override
    assert asked[0] == "pony-diffusion-v6-xl" and asked[-1] == "pixel-party-xl"


def test_forced_image_turn_sends_the_chosen_model():
    import inspect
    import routes.chat_routes as cr
    src = inspect.getsource(cr)
    assert '"model": image_model_choice' in src and "IMAGE_MODEL_OVERRIDE.set(image_model_choice)" in src


def test_quality_steps_scale_with_the_models_default():
    import importlib.util, types
    spec = importlib.util.spec_from_file_location("diffusion_server_q", "scripts/diffusion_server.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        import pytest
        pytest.skip("diffusion server deps not importable here")
    for default, expect in ((25, {"low": 15, "medium": 25, "high": 35}), (8, {"low": 4, "medium": 8, "high": 20})):
        mod._args = types.SimpleNamespace(steps=default)
        got = {q: mod._quality_steps(q) for q in ("low", "medium", "high")}
        assert got == expect and got["low"] <= got["medium"] <= got["high"]


def test_presets_add_required_tags_once():
    from src import image_presets as ip
    text, negative = ip.apply("pony-diffusion-v6-xl", "1girl, hoodie")
    assert text.startswith("score_9, score_8_up") and text.endswith("1girl, hoodie") and "bad hands" in negative
    assert ip.apply("pony-diffusion-v6-xl", text)[0] == text
    pixel = ip.apply("pixel-party-xl", "a cat.")[0]
    assert pixel == "a cat. in pixel art style" and ip.apply("pixel-party-xl", pixel)[0] == pixel
    assert ip.apply("unknown-model", "a cat") == ("a cat", "")


def test_presets_can_be_overridden_from_data(tmp_path, monkeypatch):
    import json
    from src import image_presets as ip
    monkeypatch.setattr(ip, "_data_dir", lambda: tmp_path)
    (tmp_path / "image_presets.json").write_text(json.dumps({"new-model": {"positive": "masterpiece", "tags": True}}))
    assert ip.apply("new-model", "x")[0] == "masterpiece, x" and ip.preset_for("new-model")["tags"] is True


def test_token_budget_includes_the_preset():
    from src import image_presets as ip
    count = lambda text: 2 + len(text.replace(",", " ").split())
    pony = ip.tokens("pony-diffusion-v6-xl", "1girl", count=count)
    plain = ip.tokens("cyberrealistic-xl", "1girl", count=count)
    assert pony["used"] == plain["used"] + 6 and pony["left"] == 77 - pony["used"]


def test_tag_search_prefers_popular_prefixes_then_aliases(tmp_path, monkeypatch):
    from src import image_presets as ip
    monkeypatch.setattr(ip, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(ip, "_tags", None)
    (tmp_path / "image_tags.csv").write_text('long_hair,0,400,"/lh,longhair"\nlong_sleeves,0,900,\nshort_hair,0,800,\n^_^,5,10,\n')
    assert [t["tag"] for t in ip.search("long")] == ["long sleeves", "long hair"]
    assert ip.search("^_")[0]["tag"] == "^_^"
    assert [t["tag"] for t in ip.search("/lh")] == ["long hair"] and ip.search("l") == []
    monkeypatch.setattr(ip, "_tags", None)


def test_composer_helper_is_wired_up():
    source = open("static/app.js", encoding="utf-8").read()
    assert "initImagePromptHelpers" in source and "/api/image-models/tokens" in source and "/api/image-models/tags" in source
