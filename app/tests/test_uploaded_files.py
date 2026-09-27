"""
Tests for uploaded-file download/preview and the expanded upload allowlist.

Design: keep it boring. Office/slideshow files are never rendered server-side —
they download (Content-Disposition: attachment) and the OS opens them in the real
app. Only browser-native types (raster images, PDF) are served inline. SVG is
NEVER served inline (it can carry script).
"""
import base64

import pytest

import app.routers.api as api


def _write(tmp_path, name, content=b"x"):
    p = tmp_path / name
    p.write_bytes(content)
    return p


class TestUploadAllowlist:
    @pytest.mark.parametrize("ext", [
        ".pptx", ".ppt", ".pptm",          # PowerPoint incl. macros
        ".key", ".odp",                     # Keynote, OpenDocument
        ".xlsx", ".xls", ".xlsm", ".xlsb", ".xltx", ".xltm",  # all Excel incl. macros
        ".docx", ".pdf", ".csv",
    ])
    def test_new_types_are_allowed(self, ext):
        assert ext in api.UPLOAD_ALLOWED_EXTENSIONS

    @pytest.mark.asyncio
    async def test_upload_pptx_accepted(self, async_client, tmp_path, monkeypatch):
        monkeypatch.setattr(api, "IMPORTS_DIR", str(tmp_path))
        r = await async_client.post(
            "/api/upload-to-assets",
            files={"file": ("deck.pptx", b"PK\x03\x04 fake pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
        )
        assert r.status_code == 200
        assert (tmp_path / "deck.pptx").exists()

    @pytest.mark.asyncio
    async def test_upload_xlsm_accepted(self, async_client, tmp_path, monkeypatch):
        monkeypatch.setattr(api, "IMPORTS_DIR", str(tmp_path))
        r = await async_client.post(
            "/api/upload-to-assets",
            files={"file": ("macros.xlsm", b"PK\x03\x04 fake xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12")},
        )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_upload_exe_still_rejected(self, async_client, tmp_path, monkeypatch):
        monkeypatch.setattr(api, "IMPORTS_DIR", str(tmp_path))
        r = await async_client.post(
            "/api/upload-to-assets",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        )
        assert r.status_code == 400


class TestDownloadAndPreview:
    @pytest.mark.asyncio
    async def test_download_forces_attachment(self, async_client, tmp_path, monkeypatch):
        monkeypatch.setattr(api, "IMPORTS_DIR", str(tmp_path))
        monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/imports", str(tmp_path))
        _write(tmp_path, "deck.pptx", b"PK\x03\x04")
        r = await async_client.get("/api/uploaded-files/preview?path=app/imports/deck.pptx&download=1")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        assert "deck.pptx" in r.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_office_file_preview_is_attachment_not_inline(self, async_client, tmp_path, monkeypatch):
        # Even without download=1, a non-native type must not be served inline.
        monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/imports", str(tmp_path))
        _write(tmp_path, "sheet.xlsx", b"PK\x03\x04")
        r = await async_client.get("/api/uploaded-files/preview?path=app/imports/sheet.xlsx")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_image_previews_inline(self, async_client, tmp_path, monkeypatch):
        monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/assets/images", str(tmp_path))
        _write(tmp_path, "photo.png", b"\x89PNG\r\n")
        r = await async_client.get("/api/uploaded-files/preview?path=app/assets/images/photo.png")
        assert r.status_code == 200
        assert "inline" in r.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_svg_is_never_inline(self, async_client, tmp_path, monkeypatch):
        # SVG can embed <script>; serving it inline is an XSS vector.
        monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/assets/images", str(tmp_path))
        _write(tmp_path, "x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>")
        r = await async_client.get("/api/uploaded-files/preview?path=app/assets/images/x.svg")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_path_traversal_still_blocked_on_download(self, async_client, tmp_path, monkeypatch):
        monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/imports", str(tmp_path))
        r = await async_client.get("/api/uploaded-files/preview?path=app/imports/../../etc/passwd&download=1")
        assert r.status_code == 400


class TestDeleteUploadedFile:
    """My Uploads modal → Delete. Same path rules as preview: only the two
    upload roots, no traversal, no dotfiles."""

    @pytest.mark.asyncio
    async def test_delete_removes_file(self, async_client, tmp_path, monkeypatch):
        monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/imports", str(tmp_path))
        _write(tmp_path, "old.csv", b"a,b")
        r = await async_client.delete("/api/uploaded-files?path=app/imports/old.csv")
        assert r.status_code == 200
        assert r.json()["deleted"] == "app/imports/old.csv"
        assert not (tmp_path / "old.csv").exists()

    @pytest.mark.asyncio
    async def test_delete_image(self, async_client, tmp_path, monkeypatch):
        monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/assets/images", str(tmp_path))
        _write(tmp_path, "logo.png", b"\x89PNG")
        r = await async_client.delete("/api/uploaded-files?path=app/assets/images/logo.png")
        assert r.status_code == 200
        assert not (tmp_path / "logo.png").exists()

    @pytest.mark.asyncio
    async def test_delete_missing_is_404(self, async_client, tmp_path, monkeypatch):
        monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/imports", str(tmp_path))
        r = await async_client.delete("/api/uploaded-files?path=app/imports/nope.csv")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_file_over_preview_cap(self, async_client, tmp_path, monkeypatch):
        # The 50MB cap is for previews; big files must still be deletable.
        monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/imports", str(tmp_path))
        monkeypatch.setattr(api, "MAX_PREVIEW_BYTES", 2)
        _write(tmp_path, "big.zip", b"PK\x03\x04")
        r = await async_client.delete("/api/uploaded-files?path=app/imports/big.zip")
        assert r.status_code == 200
        assert not (tmp_path / "big.zip").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [
        "app/imports/../../config/master.key",
        "app/imports/sub/x.csv",
        "app/imports/.env",
        "config/database.yml",
        "app/models/user.rb",
    ])
    async def test_delete_outside_upload_roots_blocked(self, async_client, tmp_path, monkeypatch, bad):
        monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/imports", str(tmp_path))
        r = await async_client.delete(f"/api/uploaded-files?path={bad}")
        assert r.status_code == 400
