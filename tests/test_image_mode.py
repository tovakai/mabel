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
