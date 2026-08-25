"""Spreadsheet previews are parsed on the box, not by Microsoft.

The asset library used to offer "Preview with Microsoft Office Online", which
embedded view.officeapps.live.com in an iframe. That viewer fetches the file
from Microsoft's servers, so it can never see an auth-gated
/api/uploaded-files/preview URL — it was broken by construction. Spreadsheets
now render as a plain cell grid from /api/uploaded-files/sheet (openpyxl).
"""
import csv

import pytest
from openpyxl import Workbook

import app.routers.api as api


def _xlsx(tmp_path, name="book.xlsx", sheets=None):
    sheets = sheets or {"Sheet1": [["a", 1], ["b", 2]]}
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
    path = tmp_path / name
    wb.save(path)
    return path


@pytest.fixture
def imports_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(api.PREVIEW_PATH_PREFIXES, "app/imports", str(tmp_path))
    return tmp_path


class TestSheetEndpoint:
    @pytest.mark.asyncio
    async def test_xlsx_returns_cell_rows(self, async_client, imports_dir):
        _xlsx(imports_dir, sheets={"Data": [["Name", "Qty"], ["Widget", 3]]})
        r = await async_client.get("/api/uploaded-files/sheet?path=app/imports/book.xlsx")
        assert r.status_code == 200
        body = r.json()
        assert body["sheets"] == ["Data"]
        assert body["rows"] == [["Name", "Qty"], ["Widget", "3"]]
        assert body["truncated"] is False

    @pytest.mark.asyncio
    async def test_sheet_index_selects_the_tab(self, async_client, imports_dir):
        _xlsx(imports_dir, sheets={"First": [["x"]], "Second": [["y"]]})
        r = await async_client.get("/api/uploaded-files/sheet?path=app/imports/book.xlsx&sheet=1")
        assert r.status_code == 200
        body = r.json()
        assert body["active"] == 1
        assert body["rows"] == [["y"]]

    @pytest.mark.asyncio
    async def test_out_of_range_sheet_falls_back_to_first(self, async_client, imports_dir):
        _xlsx(imports_dir, sheets={"First": [["x"]]})
        r = await async_client.get("/api/uploaded-files/sheet?path=app/imports/book.xlsx&sheet=9")
        assert r.status_code == 200
        assert r.json()["active"] == 0

    @pytest.mark.asyncio
    async def test_row_cap_reports_the_full_count(self, async_client, imports_dir):
        rows = [[i] for i in range(api.SHEET_PREVIEW_MAX_ROWS + 25)]
        _xlsx(imports_dir, sheets={"Big": rows})
        r = await async_client.get("/api/uploaded-files/sheet?path=app/imports/book.xlsx")
        body = r.json()
        assert len(body["rows"]) == api.SHEET_PREVIEW_MAX_ROWS
        assert body["total_rows"] == api.SHEET_PREVIEW_MAX_ROWS + 25
        assert body["truncated"] is True

    @pytest.mark.asyncio
    async def test_csv_is_parsed_too(self, async_client, imports_dir):
        p = imports_dir / "rows.csv"
        with open(p, "w", newline="") as fh:
            csv.writer(fh).writerows([["Name", "Qty"], ["Widget", "3"]])
        r = await async_client.get("/api/uploaded-files/sheet?path=app/imports/rows.csv")
        assert r.status_code == 200
        assert r.json()["rows"] == [["Name", "Qty"], ["Widget", "3"]]

    @pytest.mark.asyncio
    async def test_tsv_uses_tab_delimiter(self, async_client, imports_dir):
        (imports_dir / "rows.tsv").write_text("Name\tQty\nWidget\t3\n")
        r = await async_client.get("/api/uploaded-files/sheet?path=app/imports/rows.tsv")
        assert r.status_code == 200
        assert r.json()["rows"] == [["Name", "Qty"], ["Widget", "3"]]

    @pytest.mark.asyncio
    async def test_legacy_xls_returns_415_for_client_fallback(self, async_client, imports_dir):
        # openpyxl can't read the legacy binary format; 415 tells the frontend to
        # fall back to its client-side reader rather than showing an error.
        (imports_dir / "old.xls").write_bytes(b"\xd0\xcf\x11\xe0")
        r = await async_client.get("/api/uploaded-files/sheet?path=app/imports/old.xls")
        assert r.status_code == 415

    @pytest.mark.asyncio
    async def test_corrupt_workbook_is_422_not_500(self, async_client, imports_dir):
        (imports_dir / "broken.xlsx").write_bytes(b"not a zip file at all")
        r = await async_client.get("/api/uploaded-files/sheet?path=app/imports/broken.xlsx")
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, async_client, imports_dir):
        r = await async_client.get("/api/uploaded-files/sheet?path=app/imports/../../etc/passwd")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_file_is_404(self, async_client, imports_dir):
        r = await async_client.get("/api/uploaded-files/sheet?path=app/imports/nope.xlsx")
        assert r.status_code == 404


class TestNoThirdPartyViewer:
    def test_office_online_iframe_is_gone_from_the_frontend(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        js = (repo_root / "app/frontend/chat/ui/FileAttachmentManager.js").read_text()
        assert "officeapps.live.com" not in js
        assert "asset-office-btn" not in js
