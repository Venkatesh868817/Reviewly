"""Offline tests for the scraper. No real network calls are made.

Run with:  python -m unittest -v test_app
"""
import json
import unittest
from unittest import mock

from bs4 import BeautifulSoup

import app as reviewly


def flipkart_html(reviews):
    components = [{"value": r} for r in reviews]
    state = {
        "multiWidgetState": {
            "widgetsData": {
                "slots": [
                    {"slotData": {"widget": {"type": "OTHER", "data": {}}}},
                    {"slotData": None},  # a null slot must not crash parsing
                    {
                        "slotData": {
                            "widget": {
                                "type": "REVIEWS",
                                "data": {"renderableComponents": components},
                            }
                        }
                    },
                ]
            }
        }
    }
    return f"<html><script>window.__INITIAL_STATE__ = {json.dumps(state)};</script></html>"


def fk_review(i, rating=5, text=None):
    return {"id": f"r{i}", "text": text or f"Great phone number {i}", "title": "Nice", "rating": rating, "created": "Jan 2026"}


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    @property
    def is_redirect(self):
        return "Location" in self.headers and self.status_code in {301, 302, 303, 307, 308}

    def iter_content(self, size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RatingTests(unittest.TestCase):
    def test_basic_and_scaled(self):
        self.assertEqual(reviewly.rating_value("4/5"), 4.0)
        self.assertEqual(reviewly.rating_value({"ratingValue": 8, "bestRating": 10}), 4.0)
        self.assertEqual(reviewly.rating_value(3), 3.0)

    def test_invalid(self):
        for bad in (None, "", "nan", "abc", 7, -1, "1,234 ratings"):
            self.assertIsNone(reviewly.rating_value(bad), bad)

    def test_node_rating_ignores_counts(self):
        node = BeautifulSoup(
            '<div><span class="rating-count">1,234 ratings</span>'
            '<i aria-label="4 out of 5 stars"></i></div>',
            "html.parser",
        )
        self.assertEqual(reviewly.rating_from_node(node), 4.0)
        no_stars = BeautifulSoup('<div><span class="rating-count">1,234 ratings</span></div>', "html.parser")
        self.assertIsNone(reviewly.rating_from_node(no_stars))


class GenericParsingTests(unittest.TestCase):
    def test_jsonld_graph(self):
        data = {"@graph": [{"@type": "Product", "review": [
            {"@type": "Review", "reviewBody": "Love it", "reviewRating": {"ratingValue": "5"}, "author": {"name": "A"}},
            {"@type": ["Review"], "name": "Bad", "reviewRating": {"ratingValue": 2, "bestRating": 5}},
        ]}]}
        found = reviewly.find_jsonld_reviews(data)
        self.assertEqual([r["rating"] for r in found], [5.0, 2.0])

    def test_wrapper_and_nested_nodes_are_not_duplicated(self):
        html = '<div class="review-card-list">' + "".join(
            f'<div class="review-card"><span class="review-item-title">T{i}</span>'
            f'<p class="review-text">Review body number {i} is long enough</p></div>'
            for i in range(3)
        ) + "</div>"
        nodes = reviewly.outermost_review_nodes(BeautifulSoup(html, "html.parser"))
        self.assertEqual(len(nodes), 3)

    def test_scrape_reviews_from_html(self):
        html = b"""<html><body>
        <div data-hook="review"><span data-hook="review-body">Really love this thing a lot</span>
        <i aria-label="5 out of 5 stars"></i></div>
        <div data-hook="review"><span data-hook="review-body">Terrible, it broke quickly</span>
        <i aria-label="1 out of 5 stars"></i></div></body></html>"""
        with mock.patch.object(reviewly, "safe_get", return_value=("u", html, {})):
            reviews = reviewly.scrape_reviews("https://shop.example/p")
        self.assertEqual([r["rating"] for r in reviews], [5.0, 1.0])

    def test_no_reviews_raises(self):
        with mock.patch.object(reviewly, "safe_get", return_value=("u", b"<html></html>", {})):
            with self.assertRaises(reviewly.ScrapeError):
                reviewly.scrape_reviews("https://shop.example/p")


class FlipkartTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(reviewly.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = "https://www.flipkart.com/some-phone/p/itm123?pid=MOB1&lid=LST1"

    def test_parses_dedups_and_builds_urls(self):
        pages = {
            ("MOST_HELPFUL", "1"): [fk_review(1), fk_review(2)],
            ("MOST_HELPFUL", "2"): [fk_review(2), fk_review(3)],
            ("MOST_HELPFUL", "3"): [fk_review(3)],            # nothing new -> stop this sort
            ("MOST_RECENT", "1"): [fk_review(4, rating=2)],
            ("MOST_RECENT", "2"): [fk_review(4, rating=2)],   # nothing new -> stop
        }
        seen_urls = []

        def fake_fetch(page_url):
            seen_urls.append(page_url)
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(page_url).query)
            return flipkart_html(pages[(qs["sortOrder"][0], qs["page"][0])])

        with mock.patch.object(reviewly, "fetch_flipkart_page", side_effect=fake_fetch):
            reviews = reviewly.scrape_flipkart_reviews(self.url)

        self.assertEqual(len(reviews), 4)
        self.assertTrue(all("/product-reviews/itm123" in u and "pid=MOB1" in u for u in seen_urls))
        self.assertEqual(reviews[-1]["rating"], 2.0)

    def test_blocked_first_page_gives_clear_error(self):
        with mock.patch.object(reviewly, "fetch_flipkart_page", return_value="<html>Are you a human? captcha</html>"):
            with self.assertRaises(reviewly.ScrapeError) as ctx:
                reviewly.scrape_flipkart_reviews(self.url)
        self.assertIn("bot-check", str(ctx.exception))

    def test_layout_change_message(self):
        html = '<script>window.__INITIAL_STATE__ = {"multiWidgetState": {}};</script>'
        with mock.patch.object(reviewly, "fetch_flipkart_page", return_value=html):
            with self.assertRaises(reviewly.ScrapeError) as ctx:
                reviewly.scrape_flipkart_reviews(self.url)
        self.assertIn("no reviews widget", str(ctx.exception))

    def test_rejects_other_hosts_and_non_product_paths(self):
        with self.assertRaises(ValueError):
            reviewly.scrape_flipkart_reviews("https://notflipkart.com/x/p/itm1")
        with self.assertRaises(ValueError):
            reviewly.scrape_flipkart_reviews("https://www.flipkart.com/search?q=phone")


class SafeGetTests(unittest.TestCase):
    def test_redirect_to_private_address_is_blocked(self):
        def fake_validate(u):
            if "169.254" in u:
                raise ValueError("The URL must point to a publicly accessible website.")
            return u

        responses = [FakeResponse(302, {"Location": "http://169.254.169.254/latest"})]
        with mock.patch.object(reviewly, "validate_public_url", side_effect=fake_validate), \
             mock.patch.object(reviewly.http, "get", side_effect=responses):
            with self.assertRaises(ValueError):
                reviewly.safe_get("https://shop.example/p")

    def test_follows_http_to_https_redirect(self):
        responses = [
            FakeResponse(301, {"Location": "https://shop.example/p"}),
            FakeResponse(200, {"Content-Type": "text/html"}, b"<html>ok</html>"),
        ]
        with mock.patch.object(reviewly, "validate_public_url", side_effect=lambda u: u), \
             mock.patch.object(reviewly.http, "get", side_effect=responses):
            final, body, _ = reviewly.safe_get("http://shop.example/p", require_html=True)
        self.assertEqual(final, "https://shop.example/p")
        self.assertEqual(body, b"<html>ok</html>")

    def test_flipkart_cannot_redirect_off_domain(self):
        responses = [FakeResponse(302, {"Location": "https://evil.example/x"})]
        with mock.patch.object(reviewly, "validate_public_url", side_effect=lambda u: u), \
             mock.patch.object(reviewly.http, "get", side_effect=responses):
            with self.assertRaises(reviewly.ScrapeError):
                reviewly.safe_get("https://www.flipkart.com/x", host_ok=reviewly.is_flipkart_host)

    def test_http_errors_are_specific_and_size_is_capped(self):
        with mock.patch.object(reviewly, "validate_public_url", side_effect=lambda u: u), \
             mock.patch.object(reviewly.http, "get", return_value=FakeResponse(403)):
            with self.assertRaises(reviewly.ScrapeError) as ctx:
                reviewly.safe_get("https://shop.example/p")
        self.assertEqual(ctx.exception.http_status, 403)

        big = FakeResponse(200, {"Content-Type": "text/html"}, b"x" * (reviewly.MAX_PAGE_BYTES + 1))
        with mock.patch.object(reviewly, "validate_public_url", side_effect=lambda u: u), \
             mock.patch.object(reviewly.http, "get", return_value=big):
            with self.assertRaises(reviewly.ScrapeError):
                reviewly.safe_get("https://shop.example/p")

    def test_flipkart_direct_403_mentions_token(self):
        with mock.patch.object(reviewly, "SCRAPEDO_TOKEN", ""), \
             mock.patch.object(reviewly, "safe_get", side_effect=reviewly.status_error(403)):
            with self.assertRaises(reviewly.ScrapeError) as ctx:
                reviewly.fetch_flipkart_page("https://www.flipkart.com/x")
        self.assertIn("SCRAPEDO_TOKEN", str(ctx.exception))

    def test_scrapedo_fetch_requests_javascript_rendering(self):
        response = FakeResponse(200, {"Content-Type": "text/html; charset=utf-8"}, b"<html>ok</html>")
        with mock.patch.object(reviewly, "SCRAPEDO_TOKEN", "test-token"), \
             mock.patch.object(reviewly.requests, "get", return_value=response) as get:
            html = reviewly.fetch_flipkart_page("https://www.flipkart.com/x")

        self.assertEqual(html, "<html>ok</html>")
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["render"], "true")
        self.assertEqual(params["customWait"], "2000")
        self.assertEqual(params["geoCode"], "in")


class ValidationTests(unittest.TestCase):
    def test_private_and_bad_urls_rejected(self):
        with mock.patch.object(reviewly.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 443))]):
            with self.assertRaises(ValueError):
                reviewly.validate_public_url("https://internal.example/")
        for bad in ("ftp://x.com", "https://user:pw@x.com", "https://x.com:8080/", "not a url"):
            with self.assertRaises(ValueError, msg=bad):
                reviewly.validate_public_url(bad)


class AnalyzeAndRouteTests(unittest.TestCase):
    def test_analyze(self):
        reviews = [
            {"review": "Great battery life", "rating": 5.0, "date": "", "author": ""},
            {"review": "Terrible battery drain", "rating": 1.0, "date": "", "author": ""},
        ]
        result = reviewly.analyze(reviews)
        self.assertEqual(result["total_reviews"], 2)
        self.assertEqual(result["rating_positive_percentage"], 50.0)
        self.assertEqual(result["top_words"][0]["word"], "battery")
        self.assertIsNotNone(result["sample_warning"])
        with self.assertRaises(reviewly.ScrapeError):
            reviewly.analyze([])

    def test_route_returns_json_for_unexpected_errors_and_caches(self):
        client = reviewly.app.test_client()
        with mock.patch.object(reviewly, "validate_public_url", side_effect=lambda u: u), \
             mock.patch.object(reviewly, "scrape_reviews", side_effect=RuntimeError("boom")):
            response = client.post("/api/analyze", json={"url": "https://shop.example/crash"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("error", response.get_json())

        good = [{"review": "Great", "rating": 5.0, "date": "", "author": ""}]
        with mock.patch.object(reviewly, "validate_public_url", side_effect=lambda u: u), \
             mock.patch.object(reviewly, "scrape_reviews", return_value=good) as scraper:
            first = client.post("/api/analyze", json={"url": "https://shop.example/ok"})
            second = client.post("/api/analyze", json={"url": "https://shop.example/ok"})
            third = client.post("/api/analyze", json={"url": "https://shop.example/ok", "refresh": True})
        self.assertEqual(first.status_code, 200)
        self.assertTrue(second.get_json().get("cached"))
        self.assertEqual(scraper.call_count, 2)  # first + refresh; second was cached
        self.assertEqual(third.status_code, 200)


if __name__ == "__main__":
    unittest.main()
