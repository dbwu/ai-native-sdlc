"""验证本地工作台可作为同源页面交付，不依赖外部脚本或字体服务。"""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from fastapi.testclient import TestClient

from knowledge.api import create_app


class Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.urls.append(values["src"])
        if tag == "link" and values.get("href"):
            self.urls.append(values["href"])


def test_workbench_entry_and_assets_are_served_locally():
    with TestClient(create_app("unused")) as client:
        response = client.get("/workbench/")
        assert response.status_code == 200, response.text
        assert "text/html" in response.headers["content-type"]
        assert "工程知识" in response.text
        parser = Resources()
        parser.feed(response.text)
        assert parser.urls
        assert all(not url.startswith(("http:", "https:", "//")) for url in parser.urls)
        script_count = 0
        for asset in parser.urls:
            resource_path = urljoin("/workbench/", asset)
            assert resource_path.startswith("/workbench/"), resource_path
            asset_response = client.get(resource_path)
            assert asset_response.status_code == 200, asset_response.text
            suffix = urlsplit(resource_path).path.rsplit(".", 1)[-1]
            if suffix in {"js", "mjs"}:
                assert "javascript" in asset_response.headers["content-type"]
                script_count += 1
            elif suffix == "css":
                assert "text/css" in asset_response.headers["content-type"]
        assert script_count > 0


def test_api_docs_and_contract_remain_available_alongside_the_workbench():
    with TestClient(create_app("unused")) as client:
        assert client.get("/docs").status_code == 200
        response = client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        assert "/entities" in paths
        assert "/workflows" in paths
        assert "/workbench/projects" in paths
