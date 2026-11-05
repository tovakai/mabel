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
    assert 'json.dumps({"prompt": _user_msg, "size": "1024x1024"})' in src
