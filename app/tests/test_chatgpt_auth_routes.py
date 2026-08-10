"""Routes and UI wiring for connecting a user's own ChatGPT plan.

The behaviours pinned here are the ones that would silently strand a user:
a blocked auth host that looks like a retryable error, a status endpoint that
leaks token material, and modal markup whose handlers don't match (the SI#203
class of bug — a dead button nobody notices for a month).
"""

import re
from pathlib import Path

import pytest

CHAT_HTML = Path(__file__).resolve().parents[1] / "frontend" / "chat.html"


@pytest.fixture
def html():
    return CHAT_HTML.read_text()


# --- the blocked-host path ---------------------------------------------------


def test_start_reports_blocked_host_distinctly(monkeypatch):
    """A Cloudflare block is NOT a retryable error. The UI has to be able to tell
    the difference so it can offer the paste flow instead of a 'try again' that
    can never succeed."""
    import asyncio

    from fastapi import HTTPException

    from app.routers import chatgpt_auth as routes
    from app.services import chatgpt_auth as service
    from app.services import codex_cli_auth

    async def blocked():
        raise service.AuthHostBlocked("auth.openai.com refused this server's IP address")

    # This is the CLI-less fallback path; with the CLI present /start never
    # reaches it (see test_start_prefers_the_codex_cli).
    monkeypatch.setattr(codex_cli_auth, "cli_available", lambda: False)
    monkeypatch.setattr(service, "request_device_code", blocked)

    user = type("U", (), {"id": 1})()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.start(user=user))

    assert exc.value.status_code == 503
    assert exc.value.detail["reason"] == "auth_host_blocked"


def test_start_prefers_the_codex_cli(monkeypatch):
    """The CLI is the only client that gets past the Cloudflare challenge, and it
    owns refresh — a login that skipped it would die at first expiry."""
    import asyncio

    from app.routers import chatgpt_auth as routes
    from app.services import chatgpt_auth as service
    from app.services import codex_cli_auth

    async def never_called():
        raise AssertionError("/start used the HTTP path while the CLI was available")

    async def device(_user_id):
        return {"verification_url": "https://auth.openai.com/codex/device",
                "user_code": "ABCD-1234", "interval": 5}

    monkeypatch.setattr(codex_cli_auth, "cli_available", lambda: True)
    monkeypatch.setattr(codex_cli_auth, "start_device_login", device)
    monkeypatch.setattr(service, "request_device_code", never_called)

    result = asyncio.run(routes.start(user=type("U", (), {"id": 1})()))

    assert result["user_code"] == "ABCD-1234"


def test_auth_host_blocked_detects_cloudflare_challenge():
    """Recognise the challenge by status + content-type, not by body text, so a
    reworded interstitial doesn't turn back into a mystery 403."""
    from app.services.chatgpt_auth import AuthHostBlocked, _raise_if_blocked

    class Resp:
        status_code = 403
        headers = {"content-type": "text/html; charset=UTF-8"}

    with pytest.raises(AuthHostBlocked):
        _raise_if_blocked(Resp())


def test_json_403_is_not_treated_as_a_network_block():
    """A genuine 403 from OpenAI (bad client_id, revoked grant) must stay a normal
    auth error — otherwise we'd tell users to go paste a token for no reason."""
    from app.services.chatgpt_auth import _raise_if_blocked

    class Resp:
        status_code = 403
        headers = {"content-type": "application/json"}

    _raise_if_blocked(Resp())  # must not raise


# --- no secrets over the wire ------------------------------------------------


def test_status_never_returns_token_material():
    from app.services.chatgpt_auth import status_for_user

    class Row:
        disconnected_reason = None
        account_email = "kody@example.com"
        plan_tier = "pro"
        expires_at = None
        access_token_encrypted = "ENCRYPTED-ACCESS"
        refresh_token_encrypted = "ENCRYPTED-REFRESH"

    import app.services.chatgpt_auth as svc

    original = svc.get_credential
    svc.get_credential = lambda _s, _u: Row()
    try:
        payload = status_for_user(None, 1)
    finally:
        svc.get_credential = original

    flat = repr(payload)
    assert "ENCRYPTED-ACCESS" not in flat
    assert "ENCRYPTED-REFRESH" not in flat
    assert set(payload) <= {
        "connected", "account_email", "plan_tier", "expires_at", "disconnected_reason",
        "auth_method", "can_auto_refresh", "expires_in_seconds",
    }


# --- how the account was connected -------------------------------------------
#
# A pasted credential and a device-code one look identical once stored, but only
# one of them can renew itself. Left unsaid, the pasted one expires and the user
# is silently dropped back to the default model mid-task with no explanation —
# the exact failure this feature exists to remove.


class _Row:
    disconnected_reason = None
    account_email = "kody@example.com"
    plan_tier = "plus"
    expires_at = None
    access_token_encrypted = "x"
    refresh_token_encrypted = "y"


def _status_with(monkeypatch, *, cli_available, login_finished):
    import app.services.chatgpt_auth as svc
    from app.services import codex_cli_auth

    monkeypatch.setattr(svc, "get_credential", lambda _s, _u: _Row())
    monkeypatch.setattr(codex_cli_auth, "cli_available", lambda: cli_available)
    monkeypatch.setattr(codex_cli_auth, "login_finished", lambda _u: login_finished)
    return svc.status_for_user(None, 1)


def test_cli_backed_login_reports_that_it_renews_itself(monkeypatch):
    status = _status_with(monkeypatch, cli_available=True, login_finished=True)

    assert status["auth_method"] == "device_code"
    assert status["can_auto_refresh"] is True


def test_pasted_credential_admits_it_cannot_renew(monkeypatch):
    """The user has to be told BEFORE it expires, not after."""
    status = _status_with(monkeypatch, cli_available=True, login_finished=False)

    assert status["auth_method"] == "pasted_token"
    assert status["can_auto_refresh"] is False


def test_image_without_the_cli_still_refreshes_over_http(monkeypatch):
    """Older images have no codex binary and fall back to our own HTTP refresh,
    which works wherever auth.openai.com isn't challenging us."""
    status = _status_with(monkeypatch, cli_available=False, login_finished=False)

    assert status["can_auto_refresh"] is True


def test_status_survives_an_unreadable_cli_state_dir(monkeypatch):
    """Connection status is on the page-load path; it must never 500."""
    import app.services.chatgpt_auth as svc
    from app.services import codex_cli_auth

    def boom(_user_id):
        raise OSError("permission denied")

    monkeypatch.setattr(svc, "get_credential", lambda _s, _u: _Row())
    monkeypatch.setattr(codex_cli_auth, "login_finished", boom)

    assert svc.status_for_user(None, 1)["connected"] is True


def test_disconnected_status_has_the_same_shape(monkeypatch):
    """The UI reads these keys unconditionally."""
    import app.services.chatgpt_auth as svc

    monkeypatch.setattr(svc, "get_credential", lambda _s, _u: None)
    status = svc.status_for_user(None, 1)

    assert status["connected"] is False
    assert "auth_method" in status and "can_auto_refresh" in status


def test_import_rejects_a_blob_with_no_access_token():
    from app.services.chatgpt_auth import DeviceCodeError, import_credential

    with pytest.raises(DeviceCodeError):
        import_credential(None, 1, {"tokens": {"refresh_token": "only-a-refresh"}})


# --- UI wiring ---------------------------------------------------------------


REQUIRED_HANDLES = [
    "chatgpt-modal",
    "chatgpt-state-intro",
    "chatgpt-state-code",
    "chatgpt-state-paste",
    "chatgpt-state-connected",
    "chatgpt-state-error",
    "chatgpt-connect-btn",
    "chatgpt-paste-input",
    "chatgpt-paste-submit",
    "chatgpt-disconnect",
    "chatgpt-usercode",
    "chatgpt-verification-url",
]


@pytest.mark.parametrize("handle", REQUIRED_HANDLES)
def test_every_handle_the_script_uses_exists_in_the_markup(handle, html):
    """Both halves must be present: a handle referenced by JS but absent from the
    DOM is a silently dead control (SI#203)."""
    assert f'data-llamabot="{handle}"' in html, f"{handle} is missing from chat.html"


def test_script_only_queries_handles_that_exist(html):
    """The reverse direction: no qs('chatgpt-…') for markup we never rendered."""
    referenced = set(re.findall(r"qs\('(chatgpt-[a-z-]+)'\)", html))
    referenced |= set(re.findall(r"data-llamabot=\\\"(chatgpt-[a-z-]+)\\\"", html))
    for name in referenced:
        # state panels are resolved by template string, covered by the list above
        if name.startswith("chatgpt-state-"):
            continue
        assert f'data-llamabot="{name}"' in html, f"script queries {name}, markup has none"


def test_subscription_models_are_offered_in_the_dropdown(html):
    from app.agents.leonardo.llm_factory import _CHATGPT_SUBSCRIPTION_MODELS

    for model in _CHATGPT_SUBSCRIPTION_MODELS:
        assert f'value="{model}"' in html, f"{model} is not in the model dropdown"


def test_selecting_a_subscription_model_prompts_to_connect(html):
    """Without this the user picks 'my ChatGPT plan', gets silently downgraded to
    the default model, and never learns why."""
    assert "endsWith('-chatgpt')" in html
    assert "/api/chatgpt-auth/status" in html


def test_subscription_models_are_never_disabled_in_the_dropdown():
    """A disabled <option> fires no change event, so disabling these would make
    the sign-in modal unreachable — the model reads "greyed out" and there is no
    way in the UI to fix it. They must stay selectable and be marked instead."""
    index_js = (CHAT_HTML.parent / "chat" / "index.js").read_text()

    assert "requiresChatGptLogin" in index_js, (
        "index.js no longer distinguishes user-credential models, so they fall "
        "into the generic disable branch and become unclickable"
    )
    branch = index_js.split("requiresChatGptLogin)", 1)[1].split("} else if", 1)[0]
    assert "option.disabled = false" in branch
    assert "(Connect account)" in branch


def test_connecting_refreshes_the_model_dropdown():
    """Otherwise a just-connected account still reads '(Connect account)' until
    the user reloads the page."""
    index_js = (CHAT_HTML.parent / "chat" / "index.js").read_text()
    assert "llamabot:chatgpt-connection-changed" in index_js
    assert "data-original-label" in index_js


def test_paste_box_is_cleared_after_submit(html):
    """Tokens must not linger in the DOM after they've been stored."""
    assert "box.value = ''" in html


def test_connect_flow_falls_back_to_paste_on_503(html):
    assert "r.status === 503" in html
    assert "show('paste')" in html


# --- knowing where you stand -------------------------------------------------


def test_a_connected_account_is_visible_without_disconnecting(html):
    """Before this there was no way to see WHICH account was connected, or to
    reach the modal again, short of disconnecting and starting over."""
    assert 'data-llamabot="chatgpt-status"' in html
    assert 'data-llamabot="chatgpt-status-open"' in html


def test_the_connected_panel_offers_an_explicit_re_sign_in(html):
    """Disconnect-then-reconnect is not a sign-in button; a stale or wrong
    account needs one step, not two."""
    assert 'data-llamabot="chatgpt-signin-again"' in html


def test_the_connected_panel_names_the_sign_in_method(html):
    assert 'data-llamabot="chatgpt-method"' in html


def test_the_device_code_can_be_copied(html):
    """The code goes to another device. Retyping 9 characters with a 15-minute
    clock is where this flow loses people."""
    assert 'data-llamabot="chatgpt-copy"' in html
    assert "clipboard.writeText" in html


def test_copying_the_code_works_outside_a_secure_context(html):
    """navigator.clipboard is undefined on plain http, which self-hosters and
    LAN boxes hit — a copy button that silently does nothing is worse than none."""
    assert "execCommand('copy')" in html


def test_the_code_itself_is_clickable_not_just_the_button(html):
    body = html.split("chatgpt-state-code", 1)[1].split("</div>", 4)[0]
    assert 'data-llamabot="chatgpt-usercode"' in body
    assert "<button" in body, "the code is not clickable, only the icon is"


def test_re_sign_in_does_not_disconnect_first(html):
    """Abandoning a re-auth must not leave the user with nothing. The CLI issues
    a fresh code over an existing credential and overwrites only on success, so
    there is no reason to delete the working one up front."""
    body = html.split("function signInAgain", 1)[1].split("\n            }", 1)[0]
    assert "disconnect" not in body


def test_settings_can_deep_link_into_the_connect_modal(html):
    """The settings page has no modal of its own; it hands off to this one via
    ?connect=chatgpt, so the chat page has to honour that param."""
    assert "params.get('connect')" in html
    assert "!== 'chatgpt'" in html


def test_settings_page_surfaces_the_connection():
    """Model sign-in that only exists inside a dropdown is undiscoverable."""
    ui = (Path(__file__).resolve().parents[1] / "routers" / "ui.py").read_text()
    settings = ui.split("async def settings_page", 1)[1].split("@router.get", 1)[0]

    assert "/api/chatgpt-auth/status" in settings, "settings page never reads the status"
    assert "connect=chatgpt" in settings, "settings page has no way into the connect flow"
