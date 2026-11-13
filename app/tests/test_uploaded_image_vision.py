"""LP-44: an image attached from Uploaded files reaches the vision model.

The paperclip sends the picture as base64. The Uploaded files browser sends only a
path, and ``_build_message_content`` dropped every attachment without data, so the
model got the file name and guessed. Leo also had no tool that could open an image,
yet would say "I'm opening it now".

Now the server reads path-only images itself, under strict path rules, and Leo has
``view_image``. The vision-off switch and ZDR model rules apply to both.
"""
import base64
import io
import logging
import os

import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from PIL import Image

from app.lib import app_images
from app.websocket.request_handler import RequestHandler

VISION_MODEL = "gemini-3-flash"
TEXT_MODEL = "deepseek-v4-flash"


def _png(width=64, height=48, colour=(220, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def rails(tmp_path, monkeypatch):
    """A Rails app root with the folders uploads land in, and a secret outside them."""
    root = tmp_path / "rails"
    for d in ("app/assets/images", "app/imports", "public/uploads", "config"):
        (root / d).mkdir(parents=True)
    (root / "app/assets/images/header.png").write_bytes(_png())
    (root / "config/master.key").write_text("SECRET")
    (tmp_path / ".env").write_text("SECRET=1")
    monkeypatch.setattr(app_images, "RAILS_ROOT", root)
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
    return root


@pytest.fixture
def handler():
    return RequestHandler(FastAPI())


def _uploaded(path="app/assets/images/header.png", model=VISION_MODEL, mime="image/png"):
    return {
        "message": "what colour is the header?\n\n<UPLOADED_FILES>\n- header.png (saved to "
                   f"{path})\n</UPLOADED_FILES>",
        "llm_model": model,
        "attachments": [{"type": "uploaded_file", "filename": os.path.basename(path),
                         "path": path, "mime_type": mime, "size": 1}],
    }


def _blocks(content, kind):
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind] \
        if isinstance(content, list) else []


def _text(content):
    if isinstance(content, str):
        return content
    return " ".join(b["text"] for b in _blocks(content, "text"))


# ---------------------------------------------------------------- FR-1: path-only image -> image block

def test_uploaded_file_image_reaches_a_vision_model(rails, handler):
    content = handler._build_message_content(_uploaded())

    images = _blocks(content, "image_url")
    assert len(images) == 1
    url = images[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == (rails / "app/assets/images/header.png").read_bytes()
    assert "<UPLOADED_FILES>" in _text(content), "the path must stay so Leo can use image_tag"


def test_paperclip_image_is_unchanged_and_sent_once(rails, handler):
    data = base64.b64encode(_png()).decode()
    message = {"message": "look", "llm_model": VISION_MODEL,
               "attachments": [{"mime_type": "image/png", "data": data, "filename": "a.png",
                                "path": "app/assets/images/header.png"}]}
    images = _blocks(handler._build_message_content(message), "image_url")
    assert [i["image_url"]["url"] for i in images] == [f"data:image/png;base64,{data}"]


def test_the_server_decides_the_type_not_the_client(rails, handler):
    jpg = io.BytesIO()
    Image.new("RGB", (10, 10)).save(jpg, format="JPEG")
    (rails / "app/assets/images/photo.png").write_bytes(jpg.getvalue())
    content = handler._build_message_content(_uploaded("app/assets/images/photo.png"))
    assert _blocks(content, "image_url")[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_a_non_image_uploaded_file_is_left_to_read_uploaded_file(rails, handler):
    (rails / "app/imports/report.pdf").write_bytes(b"%PDF-1.4")
    message = _uploaded("app/imports/report.pdf", mime="application/pdf")
    assert _blocks(handler._build_message_content(message), "image_url") == []


# ---------------------------------------------------------------- FR-4: never a silent drop

def test_text_only_model_gets_a_note_and_no_image(rails, handler, caplog):
    with caplog.at_level(logging.INFO):
        content = handler._build_message_content(_uploaded(model=TEXT_MODEL))
    assert _blocks(content, "image_url") == []
    assert f"an image was attached but {TEXT_MODEL} cannot see images" in _text(content)
    assert "skipped" in caplog.text and "text-only" in caplog.text


def test_text_only_model_note_for_the_paperclip_too(rails, handler):
    data = base64.b64encode(_png()).decode()
    message = {"message": "look", "llm_model": TEXT_MODEL,
               "attachments": [{"mime_type": "image/png", "data": data, "filename": "a.png"}]}
    content = handler._build_message_content(message)
    assert _blocks(content, "image_url") == []
    assert f"an image was attached but {TEXT_MODEL} cannot see images" in _text(content)


def test_vision_off_reads_nothing(rails, handler, monkeypatch):
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "false")
    monkeypatch.setattr(app_images, "resolve_app_image",
                        lambda p: pytest.fail("read a file with vision off"))
    assert _blocks(handler._build_message_content(_uploaded()), "image_url") == []


def test_zdr_box_with_a_non_zdr_model_reads_nothing(rails, handler, monkeypatch):
    from app.agents.leonardo import zdr
    monkeypatch.setattr(zdr, "enforced", lambda: True)
    monkeypatch.setattr(zdr, "is_zdr_compliant", lambda model: False)
    monkeypatch.setattr(app_images, "resolve_app_image",
                        lambda p: pytest.fail("read a file for a non-ZDR model on a ZDR box"))
    assert _blocks(handler._build_message_content(_uploaded()), "image_url") == []


def test_zdr_box_with_a_zdr_model_still_sees_it(rails, handler, monkeypatch):
    from app.agents.leonardo import zdr
    monkeypatch.setattr(zdr, "enforced", lambda: True)
    monkeypatch.setattr(zdr, "is_zdr_compliant", lambda model: True)
    assert len(_blocks(handler._build_message_content(_uploaded()), "image_url")) == 1


# ---------------------------------------------------------------- FR-2: path rules

def _symlink_out(rails):
    link = rails / "app/assets/images/escape.png"
    link.symlink_to(rails.parent / ".env")
    return "app/assets/images/escape.png"


@pytest.mark.parametrize("path,reason", [
    ("../../.env", "outside"),
    ("app/assets/images/../../../.env", "outside"),
    ("/etc/passwd", "absolute"),
    ("config/master.key", "not allowed"),
    ("app/assets/images/missing.png", "not found"),
])
def test_unsafe_paths_are_refused_and_logged(rails, handler, caplog, path, reason):
    with caplog.at_level(logging.WARNING):
        content = handler._build_message_content(_uploaded(path))
    assert _blocks(content, "image_url") == []
    assert "refused" in caplog.text and reason in caplog.text


def test_a_symlink_out_of_the_app_is_refused(rails, handler, caplog):
    path = _symlink_out(rails)
    with caplog.at_level(logging.WARNING):
        content = handler._build_message_content(_uploaded(path))
    assert _blocks(content, "image_url") == []
    assert "refused" in caplog.text


def test_a_text_file_named_png_is_refused_as_not_an_image(rails, handler, caplog):
    (rails / "app/assets/images/fake.png").write_text("not really")
    with caplog.at_level(logging.WARNING):
        content = handler._build_message_content(_uploaded("app/assets/images/fake.png"))
    assert _blocks(content, "image_url") == []
    assert "not an image" in caplog.text


@pytest.mark.parametrize("folder", ["app/imports", "public/uploads", "public"])
def test_the_other_allowed_folders_work(rails, folder):
    (rails / folder / "x.png").write_bytes(_png())
    assert app_images.load_app_image(f"{folder}/x.png")["mime_type"] == "image/png"


def test_webp_and_gif_are_accepted(rails):
    for fmt, mime in (("WEBP", "image/webp"), ("GIF", "image/gif")):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, format=fmt)
        (rails / f"app/assets/images/x.{fmt.lower()}").write_bytes(buf.getvalue())
        assert app_images.load_app_image(f"app/assets/images/x.{fmt.lower()}")["mime_type"] == mime


# ---------------------------------------------------------------- FR-3: downscale

def test_a_large_image_is_downscaled_and_logged(rails, handler, caplog):
    (rails / "app/assets/images/big.png").write_bytes(_png(6000, 4000))
    with caplog.at_level(logging.INFO):
        content = handler._build_message_content(_uploaded("app/assets/images/big.png"))
    url = _blocks(content, "image_url")[0]["image_url"]["url"]
    sent = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(sent.size) <= 2000
    assert sent.size[0] / sent.size[1] == pytest.approx(1.5, rel=0.01)
    assert "downscaled" in caplog.text and "6000x4000" in caplog.text and "2000x1333" in caplog.text


def test_a_heavy_but_small_image_is_shrunk_under_the_byte_limit(rails, monkeypatch):
    monkeypatch.setattr(app_images, "MAX_BYTES", 20_000)
    noisy = Image.frombytes("RGB", (1200, 900), os.urandom(1200 * 900 * 3))
    buf = io.BytesIO()
    noisy.save(buf, format="PNG")
    (rails / "app/assets/images/noise.png").write_bytes(buf.getvalue())
    image = app_images.load_app_image("app/assets/images/noise.png")
    assert len(base64.b64decode(image["data"])) <= 20_000


def test_logs_never_contain_image_bytes(rails, handler, caplog):
    with caplog.at_level(logging.DEBUG):
        handler._build_message_content(_uploaded())
    data = base64.b64encode((rails / "app/assets/images/header.png").read_bytes()).decode()
    assert data[:40] not in caplog.text


# ---------------------------------------------------------------- FR-5: view_image

def _view(path, model=VISION_MODEL):
    from app.agents.leonardo.rails_agent.tools import view_image
    from types import SimpleNamespace
    runtime = SimpleNamespace(tool_call_id="call-1", state={"llm_model": model})
    return view_image.func(path=path, runtime=runtime)


def test_view_image_shows_the_model_the_picture(rails):
    update = _view("app/assets/images/header.png").update
    tool_msg, picture = update["messages"]
    assert isinstance(tool_msg, ToolMessage) and tool_msg.tool_call_id == "call-1"
    assert "base64" not in tool_msg.content
    assert isinstance(picture, HumanMessage)
    images = _blocks(picture.content, "image_url")
    assert len(images) == 1 and images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert picture.additional_kwargs.get("view_image") is True, "hidden from the chat bubble list"


def test_view_image_follows_the_path_rules(rails, monkeypatch):
    update = _view("../../.env").update
    assert len(update["messages"]) == 1
    assert "can't open" in update["messages"][0].content.lower()
    assert "SECRET" not in update["messages"][0].content


def test_view_image_on_a_text_only_model_says_so(rails, monkeypatch):
    monkeypatch.setattr(app_images, "resolve_app_image",
                        lambda p: pytest.fail("read a file for a text-only model"))
    update = _view("app/assets/images/header.png", model=TEXT_MODEL).update
    assert len(update["messages"]) == 1
    assert "cannot see images" in update["messages"][0].content


def test_view_image_respects_vision_off(rails, monkeypatch):
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "false")
    update = _view("app/assets/images/header.png").update
    assert len(update["messages"]) == 1
    assert "not enabled" in update["messages"][0].content


def test_view_image_is_in_the_rails_agent_tool_list():
    from app.agents.leonardo.rails_agent import nodes
    assert "view_image" in {t.name for t in nodes.default_tools}


def test_the_directive_points_at_view_image_and_the_images_folder():
    from app.agents.leonardo.rails_agent.prompts import UPLOADED_FILES_DIRECTIVE
    assert "app/assets/images" in UPLOADED_FILES_DIRECTIVE
    assert "view_image" in UPLOADED_FILES_DIRECTIVE
    assert "never" in UPLOADED_FILES_DIRECTIVE.lower()


# ---------------------------------------------------------------- FR-8: replay to a text-only model

def test_replayed_history_with_resolved_images_is_scrubbed_for_a_text_only_model(rails, handler):
    from app.agents.leonardo.rails_agent.middleware import StripUnsupportedMultimodalMiddleware
    sent = HumanMessage(content=handler._build_message_content(_uploaded()))
    viewed = _view("app/assets/images/header.png").update["messages"][1]
    history = [sent, AIMessage(content="red"), viewed, HumanMessage(content="and now?")]

    mw = StripUnsupportedMultimodalMiddleware()
    scrubbed = [mw._strip_content(m.content, {"images": False}, TEXT_MODEL)[0] for m in history]

    for content in scrubbed:
        assert _blocks(content, "image_url") == []


# ---------------------------------------------------------------- the stream still shows the tool row

def test_the_stream_keeps_the_view_image_tool_row():
    """Found taking the LP-44 screenshots: the stream skips any update ending in a human
    message (a queued user message), and view_image's picture IS one — so the tool row
    vanished from the live chat. Drop only the picture, keep the ToolMessage."""
    from app.websocket.request_handler import _without_view_image_tail
    tool = ToolMessage("Opened x.png.", tool_call_id="c1")
    picture = HumanMessage(content=[{"type": "text", "text": "[view_image: x.png]"}],
                           additional_kwargs={"view_image": True})
    queued = HumanMessage(content="also make it blue")

    assert _without_view_image_tail([tool, picture]) == [tool]
    assert _without_view_image_tail([tool, queued]) == [tool, queued], "a queued user message is still skipped"
    assert _without_view_image_tail([]) == []


def test_every_stream_site_uses_it():
    import inspect
    from app.websocket import request_handler
    src = inspect.getsource(request_handler)
    assert src.count("messages = _without_view_image_tail(agent_data['messages'])") == 3
