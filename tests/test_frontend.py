"""Public page routes and static-delivery boundaries for the lending workspace."""
from html.parser import HTMLParser
from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest

from app.config import ROOT, Settings
from app.main import create_app
from app.model import FakeClient


@pytest.fixture
def client(tmp_path):
    settings = Settings(gemini_api_key="offline-test-only", model_fast="fixed-fast", model_deep="fixed-deep",
                        db_path=str(tmp_path / "office.db"), output_dir=str(tmp_path / "documents"))
    with TestClient(create_app(settings, FakeClient())) as session:
        yield session


@pytest.mark.parametrize("route,title", [("/", "Lendix · A clearer way to lend"), ("/app", "Lendix · Workspace"), ("/office", "Lendix · Live office")])
def test_editorial_pages_are_served_with_the_unchanged_strict_csp(client, route, title):
    response = client.get(route)
    assert response.status_code == 200
    assert title in response.text
    assert response.headers["content-type"].startswith("text/html")
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "style-src 'self'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy


class StaticPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = set()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        assert tag != "style", "Inline styles violate the page's CSP"
        assert "style" not in values, "Inline style attributes violate the page's CSP"
        assert not any(key.startswith("on") for key in values), "Inline event handlers are prohibited"
        if tag == "script":
            assert values.get("src"), "Inline scripts are prohibited"
        if values.get("id"):
            self.ids.append(values["id"])
        for key in ("src", "href"):
            value = values.get(key, "")
            if value.startswith(("http:", "https:", "//")):
                assert tag == "a" and key == "href", "External scripts and assets are prohibited"
                assert value == "https://www.nonilion.com/room/emp-3j51-ge67-xbc7"
                assert values.get("target") == "_blank"
                assert {"noopener", "noreferrer"} <= set(values.get("rel", "").split())
            if value.startswith("/static/"):
                self.assets.add(value)


def test_every_html_file_is_csp_compatible_and_references_existing_assets(client):
    assets = set()
    for path in (ROOT / "static").glob("*.html"):
        page = StaticPage()
        page.feed(path.read_text())
        assert len(page.ids) == len(set(page.ids)), f"Duplicate page IDs in {path.name}"
        assets.update(page.assets)
    for path in (ROOT / "static").glob("*.css"):
        for value in re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", path.read_text()):
            assert value.startswith("/static/"), f"Nonlocal asset in {path.name}"
            assets.add(value)
    assert any(asset.endswith(".woff2") for asset in assets)
    for asset in assets:
        assert (ROOT / asset.lstrip("/")).is_file(), asset
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert response.content, asset


def test_workspace_and_office_render_untrusted_content_without_html_sinks():
    for name in ("app.js", "world.js", "landing.js"):
        source = (ROOT / "static" / name).read_text()
        assert ".innerHTML" not in source and "insertAdjacentHTML" not in source, name
        assert not re.search(r"\beval\s*\(", source), name
