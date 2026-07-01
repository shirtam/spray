import unittest

from ynet_crawler import extract_article_links, parse_article


class YnetCrawlerTests(unittest.TestCase):
    def test_extract_article_links_normalizes_and_deduplicates_urls(self) -> None:
        html = """
        <html>
          <body>
            <a href="/news/article/abc123">First article</a>
            <a href="https://www.ynet.co.il/news/article/abc123#comments">Duplicate</a>
            <a href="/article/rkskixybt">צור קשר</a>
            <a href="/home/0,7340,L-201,00.html">Not an article</a>
            <a href="javascript:void(0)">Ignored</a>
            <a href="https://example.com/news/article/not-ynet">Ignored external</a>
            <a href="/articles/0,7340,L-123456,00.html">Legacy article</a>
          </body>
        </html>
        """

        links = extract_article_links(html, "https://www.ynet.co.il/news")

        self.assertEqual(
            links,
            [
                ("https://www.ynet.co.il/news/article/abc123", "First article"),
                ("https://www.ynet.co.il/articles/0,7340,L-123456,00.html", "Legacy article"),
            ],
        )

    def test_parse_article_uses_meta_tags_and_paragraph_excerpt(self) -> None:
        html = """
        <html>
          <head>
            <meta property="og:title" content="Meta title">
            <meta property="og:description" content="Meta description">
            <meta property="article:published_time" content="2026-07-01T08:00:00+03:00">
            <meta property="article:section" content="news">
          </head>
          <body>
            <h1>Heading title</h1>
            <p>First paragraph.</p>
            <p>Second paragraph.</p>
          </body>
        </html>
        """

        article = parse_article(html, "https://www.ynet.co.il/news/article/abc123")

        self.assertEqual(article.title, "Meta title")
        self.assertEqual(article.description, "Meta description")
        self.assertEqual(article.published_at, "2026-07-01T08:00:00+03:00")
        self.assertEqual(article.section, "news")
        self.assertEqual(article.text_excerpt, "First paragraph. Second paragraph.")

    def test_parse_article_falls_back_to_json_ld(self) -> None:
        html = """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "NewsArticle",
                "headline": "JSON-LD title",
                "description": "JSON-LD description",
                "datePublished": "2026-07-01T09:00:00+03:00",
                "articleSection": "economy",
                "articleBody": "Body from structured data"
              }
            </script>
          </head>
          <body></body>
        </html>
        """

        article = parse_article(html, "https://www.ynet.co.il/economy/article/def456")

        self.assertEqual(article.title, "JSON-LD title")
        self.assertEqual(article.description, "JSON-LD description")
        self.assertEqual(article.published_at, "2026-07-01T09:00:00+03:00")
        self.assertEqual(article.section, "economy")
        self.assertEqual(article.text_excerpt, "Body from structured data")


if __name__ == "__main__":
    unittest.main()
