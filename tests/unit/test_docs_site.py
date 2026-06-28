"""Static checks for the public docs homepage."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = PROJECT_ROOT / "docs"


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"a", "link", "script", "img"}:
            return
        attr_name = "href" if tag in {"a", "link"} else "src"
        for name, value in attrs:
            if name == attr_name and value:
                self.links.append(value)


def _homepage_links() -> list[str]:
    parser = _LinkParser()
    parser.feed((DOCS_ROOT / "index.html").read_text(encoding="utf-8"))
    return parser.links


def test_docs_homepage_is_packaged_for_github_pages():
    homepage = DOCS_ROOT / "index.html"
    stylesheet = DOCS_ROOT / "assets" / "site.css"
    readme = PROJECT_ROOT / "README.md"

    assert homepage.exists()
    assert stylesheet.exists()

    html = homepage.read_text(encoding="utf-8")
    css = stylesheet.read_text(encoding="utf-8")

    assert "<title>Localize Pipeline" in html
    assert 'href="assets/site.css"' in html
    assert "localize bootstrap-pr" in html
    assert "localize memory stats" in html
    assert "GitHub Action" in html
    assert "Java .properties" in html
    assert "JSON" in html
    assert ".hero" in css
    assert "@media" in css
    assert "[docs/index.html](docs/index.html)" in readme.read_text(encoding="utf-8")


def test_docs_homepage_links_to_primary_guides():
    links = set(_homepage_links())

    assert "localization-cli.md" in links
    assert "github-action.md" in links
    assert "new-project-deployment.md" in links
    assert "../README.md" in links
    assert "../examples/generic-json/README.md" in links


def test_docs_homepage_internal_links_exist():
    for link in _homepage_links():
        parsed = urlparse(link)
        if parsed.scheme or parsed.netloc or link.startswith("#") or link.startswith("mailto:"):
            continue
        target = (DOCS_ROOT / parsed.path).resolve()
        try:
            target.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise AssertionError(f"docs homepage link escapes repository: {link}") from exc
        assert target.exists(), f"docs homepage link is broken: {link}"
