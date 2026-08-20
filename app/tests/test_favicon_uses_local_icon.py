"""Regression guard: every favicon points at the local dark icon, not S3.

The favicon used to be a white llama on a transparent background served from
S3. 96% of its opaque pixels were near-white, so on a light browser tab bar or
bookmark list the icon disappeared and only the blue eye dot showed. It was
replaced 2026-08-19 with Leo's face on a rounded #0d0d1a square (the dark
purple of the chat header), which reads against any tab-bar color.

Two things have to stay true or the old icon comes back:

  * every favicon ``<link>`` resolves to ``/frontend/leonardo-icon.png`` (the
    local file the badge manager already redraws its badges on top of), and
  * nothing in the favicon path still references the old S3 object.

The S3 object itself is still live and still correct as an in-page ``<img>``
logo on colored backgrounds, so this only guards ``rel="icon"`` links.
"""
import hashlib
import re
import struct
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

LOCAL_FAVICON = "/frontend/leonardo-icon.png"
OLD_S3_KEY = "4bmqe5iolvp84ceyk9ttz8vylrym"
OLD_WHITE_LLAMA_MD5 = "c35c271e12f6904745ea54872b2dfd0e"

# Every file that serves a favicon <link>, including the inline HTML pages in ui.py.
FAVICON_SOURCES = [
    "app/frontend/chat.html",
    "app/frontend/environment.html",
    "app/login.html",
    "app/register.html",
    "app/routers/ui.py",
]

FAVICON_LINK = re.compile(r'<link[^>]*rel="icon"[^>]*>', re.IGNORECASE)
HREF = re.compile(r'href="([^"]*)"', re.IGNORECASE)


def favicon_links(path):
    return FAVICON_LINK.findall((REPO_ROOT / path).read_text())


class TestFaviconLinks:
    @pytest.mark.parametrize("path", FAVICON_SOURCES)
    def test_every_favicon_link_uses_the_local_file(self, path):
        links = favicon_links(path)
        assert links, f"{path} serves no favicon <link> at all"
        for link in links:
            href = HREF.search(link)
            assert href, f"{path}: favicon link has no href: {link}"
            assert href.group(1) == LOCAL_FAVICON, f"{path}: {link}"

    @pytest.mark.parametrize("path", FAVICON_SOURCES)
    def test_no_favicon_link_still_points_at_s3(self, path):
        for link in favicon_links(path):
            assert OLD_S3_KEY not in link, f"{path}: {link}"

    def test_badge_manager_has_no_s3_fallback(self):
        """The onerror fallback would repaint the OLD icon on every badge redraw."""
        source = (REPO_ROOT / "app/frontend/chat/ui/FaviconBadgeManager.js").read_text()
        assert OLD_S3_KEY not in source


class TestFaviconAsset:
    ICON = "app/frontend/leonardo-icon.png"

    def test_shipped_icon_is_not_the_old_white_llama(self):
        digest = hashlib.md5((REPO_ROOT / self.ICON).read_bytes()).hexdigest()
        assert digest != OLD_WHITE_LLAMA_MD5

    def test_shipped_icon_is_a_512px_png(self):
        raw = (REPO_ROOT / self.ICON).read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", raw[16:24])  # IHDR is always first
        assert (width, height) == (512, 512)
