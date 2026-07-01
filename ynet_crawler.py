#!/usr/bin/env python3
"""Crawl Ynet article pages and write a compact JSON feed.

The crawler intentionally uses only Python's standard library so it can run in
minimal environments. It fetches one or more Ynet section pages, discovers
article links, visits a bounded number of those articles, and extracts common
metadata such as title, description, publication time, section, and an excerpt.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Callable, Iterable
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_SECTION_URLS = (
    "https://www.ynet.co.il/news",
    "https://www.ynet.co.il/economy",
    "https://www.ynet.co.il/sport",
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; ynet-crawler/1.0; "
    "+https://github.com/shirtam/spray)"
)
ARTICLE_PATH_RE = re.compile(r"/(?:article|articles)/|,7340,L-\d+", re.IGNORECASE)


@dataclass(frozen=True)
class Article:
    """A normalized article record emitted by the crawler."""

    url: str
    title: str
    description: str
    published_at: str
    section: str
    text_excerpt: str


class LinkExtractor(HTMLParser):
    """Extract links and visible anchor text from an HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return

        attrs_by_name = {name.lower(): value for name, value in attrs if value is not None}
        href = attrs_by_name.get("href")
        if href:
            self._href = href
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return

        text = clean_text(" ".join(self._text_parts))
        self.links.append((self._href, text))
        self._href = None
        self._text_parts = []


class ArticleParser(HTMLParser):
    """Extract article metadata and body snippets from an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.headings: list[str] = []
        self.paragraphs: list[str] = []
        self.title_parts: list[str] = []
        self.json_ld_blocks: list[str] = []
        self._active_text_tag: str | None = None
        self._text_parts: list[str] = []
        self._active_script_type: str | None = None
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_by_name = {name.lower(): value for name, value in attrs if value is not None}

        if tag == "meta":
            key = attrs_by_name.get("property") or attrs_by_name.get("name")
            content = attrs_by_name.get("content")
            if key and content:
                self.meta[key.lower()] = clean_text(content)
            return

        if tag in {"h1", "p", "title"}:
            self._active_text_tag = tag
            self._text_parts = []
            return

        if tag == "script":
            script_type = attrs_by_name.get("type", "").lower()
            if "ld+json" in script_type:
                self._active_script_type = script_type
                self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._active_script_type is not None:
            self._script_parts.append(data)
        elif self._active_text_tag is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if self._active_text_tag == tag:
            text = clean_text(" ".join(self._text_parts))
            if text:
                if tag == "h1":
                    self.headings.append(text)
                elif tag == "p":
                    self.paragraphs.append(text)
                elif tag == "title":
                    self.title_parts.append(text)
            self._active_text_tag = None
            self._text_parts = []
            return

        if tag == "script" and self._active_script_type is not None:
            script_text = "".join(self._script_parts).strip()
            if script_text:
                self.json_ld_blocks.append(script_text)
            self._active_script_type = None
            self._script_parts = []


def clean_text(text: str) -> str:
    """Collapse repeated whitespace and strip leading/trailing spaces."""

    return re.sub(r"\s+", " ", text).strip()


def normalize_url(href: str, base_url: str) -> str | None:
    """Return an absolute, defragmented HTTP(S) URL for a link."""

    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None

    absolute_url = urljoin(base_url, href)
    absolute_url, _fragment = urldefrag(absolute_url)
    parsed = urlparse(absolute_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return absolute_url


def is_ynet_url(url: str) -> bool:
    """Return True when a URL belongs to ynet.co.il or one of its subdomains."""

    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return host == "ynet.co.il" or host.endswith(".ynet.co.il")


def is_article_url(url: str) -> bool:
    """Return True when a URL looks like a Ynet article URL."""

    return is_ynet_url(url) and bool(ARTICLE_PATH_RE.search(urlparse(url).path))


def extract_article_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Extract unique Ynet article links from an HTML page."""

    parser = LinkExtractor()
    parser.feed(html)

    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for href, text in parser.links:
        url = normalize_url(href, base_url)
        if not url or not is_article_url(url) or url in seen:
            continue
        seen.add(url)
        links.append((url, text))

    return links


def parse_json_ld_blocks(blocks: Iterable[str]) -> dict[str, str]:
    """Best-effort extraction of article fields from JSON-LD blocks."""

    fields: dict[str, str] = {}

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return

        if not isinstance(value, dict):
            return

        candidates = {
            "title": value.get("headline") or value.get("name"),
            "description": value.get("description"),
            "published_at": value.get("datePublished") or value.get("dateCreated"),
            "section": value.get("articleSection"),
            "text_excerpt": value.get("articleBody"),
        }
        for key, candidate in candidates.items():
            if key not in fields and isinstance(candidate, str):
                cleaned = clean_text(candidate)
                if cleaned:
                    fields[key] = cleaned

        for nested_key in ("@graph", "mainEntity", "mainEntityOfPage"):
            if nested_key in value:
                visit(value[nested_key])

    for block in blocks:
        try:
            visit(json.loads(block))
        except json.JSONDecodeError:
            continue

    return fields


def parse_article(html: str, url: str, fallback_title: str = "") -> Article:
    """Extract an Article record from a fetched article page."""

    parser = ArticleParser()
    parser.feed(html)
    json_ld = parse_json_ld_blocks(parser.json_ld_blocks)

    title = first_non_empty(
        parser.meta.get("og:title"),
        parser.meta.get("twitter:title"),
        json_ld.get("title"),
        parser.headings[0] if parser.headings else "",
        fallback_title,
        parser.title_parts[0] if parser.title_parts else "",
    )
    description = first_non_empty(
        parser.meta.get("og:description"),
        parser.meta.get("description"),
        parser.meta.get("twitter:description"),
        json_ld.get("description"),
    )
    published_at = first_non_empty(
        parser.meta.get("article:published_time"),
        parser.meta.get("date"),
        json_ld.get("published_at"),
    )
    section = first_non_empty(
        parser.meta.get("article:section"),
        json_ld.get("section"),
        infer_section(url),
    )
    text_excerpt = first_non_empty(
        excerpt(parser.paragraphs),
        truncate(json_ld.get("text_excerpt", ""), 500),
        description,
    )

    return Article(
        url=url,
        title=title,
        description=description,
        published_at=published_at,
        section=section,
        text_excerpt=text_excerpt,
    )


def first_non_empty(*values: str | None) -> str:
    """Return the first non-empty text value."""

    for value in values:
        if value:
            cleaned = clean_text(value)
            if cleaned:
                return cleaned
    return ""


def infer_section(url: str) -> str:
    """Infer a section name from the first path segment of a URL."""

    path_segments = [segment for segment in urlparse(url).path.split("/") if segment]
    return path_segments[0] if path_segments else ""


def truncate(text: str, max_length: int) -> str:
    """Return text trimmed to at most max_length characters."""

    text = clean_text(text)
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "..."


def excerpt(paragraphs: Iterable[str], max_length: int = 500) -> str:
    """Build a short article excerpt from paragraph text."""

    return truncate(" ".join(paragraphs), max_length)


def fetch_url(url: str, timeout: float = 15.0) -> str:
    """Fetch a URL and return decoded HTML text."""

    request = Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "he,en;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


FetchFunc = Callable[[str, float], str]


class YnetCrawler:
    """Discover and fetch a bounded set of Ynet article records."""

    def __init__(
        self,
        start_urls: Iterable[str] = DEFAULT_SECTION_URLS,
        *,
        fetcher: FetchFunc = fetch_url,
        timeout: float = 15.0,
        delay_seconds: float = 0.5,
    ) -> None:
        self.start_urls = list(start_urls)
        self.fetcher = fetcher
        self.timeout = timeout
        self.delay_seconds = delay_seconds
        self.errors: list[str] = []

    def crawl(self, limit: int) -> list[Article]:
        """Crawl section pages and return up to limit article records."""

        discovered: dict[str, str] = {}
        for start_url in self.start_urls:
            try:
                html = self.fetcher(start_url, self.timeout)
            except Exception as exc:  # pragma: no cover - network dependent
                self.errors.append(f"{start_url}: {exc}")
                continue

            for article_url, link_text in extract_article_links(html, start_url):
                discovered.setdefault(article_url, link_text)
                if len(discovered) >= limit:
                    break
            if len(discovered) >= limit:
                break

        articles: list[Article] = []
        for index, (article_url, fallback_title) in enumerate(discovered.items()):
            if index > 0 and self.delay_seconds > 0:
                time.sleep(self.delay_seconds)
            try:
                article_html = self.fetcher(article_url, self.timeout)
                articles.append(parse_article(article_html, article_url, fallback_title))
            except Exception as exc:  # pragma: no cover - network dependent
                self.errors.append(f"{article_url}: {exc}")

        return articles


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl recent Ynet articles into JSON.")
    parser.add_argument(
        "--start-url",
        action="append",
        dest="start_urls",
        help="Ynet section URL to crawl. Can be provided more than once.",
    )
    parser.add_argument("--limit", type=int, default=10, help="Maximum articles to fetch.")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds.")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between article requests.")
    parser.add_argument(
        "--output",
        default="ynet_articles.json",
        help="Path to write JSON output.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    crawler = YnetCrawler(
        args.start_urls or DEFAULT_SECTION_URLS,
        timeout=args.timeout,
        delay_seconds=args.delay,
    )
    articles = crawler.crawl(args.limit)
    payload = [asdict(article) for article in articles]

    with open(args.output, "w", encoding="utf-8") as output_file:
        json.dump(payload, output_file, ensure_ascii=False, indent=2 if args.pretty else None)
        output_file.write("\n")

    print(f"Wrote {len(payload)} articles to {args.output}", file=sys.stderr)
    for error in crawler.errors:
        print(f"warning: {error}", file=sys.stderr)
    if not payload:
        print("warning: no articles were crawled", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
