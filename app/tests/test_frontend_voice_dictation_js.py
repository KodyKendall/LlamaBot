"""Run the dependency-free Node test for voice dictation, plus the page wiring around it."""

import subprocess
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = APP_ROOT / "frontend"


def test_voice_dictation_node_suite():
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/voice_dictation.test.mjs",
        ],
        cwd=APP_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_mic_button_sits_next_to_send_and_starts_hidden():
    html = (FRONTEND / "chat.html").read_text()
    mic = html.index('data-llamabot="mic-button"')
    send = html.index('data-llamabot="send-button"')
    assert mic < send
    # Nothing else between the two buttons.
    assert html[mic:send].count("<button") == 1
    tag = html[html.rindex("<button", 0, mic):mic + 200]
    assert "hidden" in tag


def test_chat_only_starts_dictation_when_the_server_says_so():
    js = (FRONTEND / "chat" / "index.js").read_text()
    assert "window.LLAMABOT_VOICE_DICTATION !== true" in js
    # Sending mid-dictation drops the late words rather than refilling the box.
    clear = js.index("input.value = '';")
    assert "this.voiceDictation?.cancel();" in js[clear:clear + 400]
