from __future__ import annotations

import ipaddress
import json
import math
import os
import random
import re
import socket
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from werkzeug.exceptions import HTTPException

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="")
http = requests.Session()
http.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
    }
)
sentiment_analyzer = SentimentIntensityAnalyzer()

MAX_PAGE_BYTES = 5_000_000
MAX_REVIEWS = 200
MAX_REDIRECTS = 5
MIN_RELIABLE_SAMPLE = 20
CACHE_TTL_SECONDS = 15 * 60
CACHE_MAX_ENTRIES = 64

# (sortOrder, pages). Mixing "most helpful" with "most recent" avoids a sample made
# only of the longest / most extreme reviews. Duplicates are removed by review id.
FLIPKART_PLAN = (("MOST_HELPFUL", 3), ("MOST_RECENT", 2))

SCRAPEDO_TOKEN = os.getenv("SCRAPEDO_TOKEN", "").strip()
SCRAPEDO_SUPER = os.getenv("SCRAPEDO_SUPER", "").strip().lower() in {"1", "true", "yes"}
DEBUG_DUMP_DIR = os.getenv("SCRAPE_DEBUG_DIR", "").strip()

BLOCK_MARKERS = ("captcha", "are you a human", "access denied", "unusual traffic")


# --------------------------------------------------------------------------- errors


class ScrapeError(ValueError):
    """A problem whose message is safe and useful to show to the user."""

    def __init__(self, message: str, status: int = 400, http_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


def status_error(status: int) -> ScrapeError:
    if status == 404:
        return ScrapeError(
            "The page was not found (HTTP 404). Check the link and try again.",
            422,
            status,
        )
    if status in {401, 403, 429, 451} or status >= 500:
        return ScrapeError(
            f"The site refused or could not serve the request (HTTP {status}). "
            "Shops often block automated requests.",
            422,
            status,
        )
    return ScrapeError(f"The site returned HTTP {status}.", 422, status)


def redact(text: str) -> str:
    return text.replace(SCRAPEDO_TOKEN, "***") if SCRAPEDO_TOKEN else text


# --------------------------------------------------------------------------- fetching


def validate_public_url(value: str) -> str:
    """Accept public HTTP(S) product pages and reject local network targets."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a complete http:// or https:// product URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing a username or password are not supported.")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Only standard HTTP and HTTPS ports are supported.")

    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise ValueError("The product website could not be reached.") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("The URL must point to a publicly accessible website.")
    return value


def is_flipkart_host(host: str | None) -> bool:
    host = (host or "").lower()
    return host == "flipkart.com" or host.endswith(".flipkart.com")


def read_capped(response: requests.Response) -> bytes:
    """Read a streamed body, stopping as soon as it exceeds MAX_PAGE_BYTES."""
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > MAX_PAGE_BYTES:
        raise ScrapeError("The page is too large to analyze.")
    chunks, size = [], 0
    for chunk in response.iter_content(64_000):
        size += len(chunk)
        if size > MAX_PAGE_BYTES:
            raise ScrapeError("The page is too large to analyze.")
        chunks.append(chunk)
    return b"".join(chunks)


def decode_body(body: bytes, headers) -> str:
    match = re.search(r"charset=([\w-]+)", headers.get("Content-Type", ""), re.I)
    for encoding in (match.group(1) if match else None, "utf-8"):
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def safe_get(url: str, *, require_html: bool = False, host_ok=None):
    """GET a URL, following redirects by hand.

    Every hop is re-validated, so a public page cannot redirect us to a private
    address. Returns (final_url, body_bytes, response_headers).
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current)
        if host_ok and not host_ok(urlparse(current).hostname):
            raise ScrapeError("The site redirected to an unexpected domain.")
        with http.get(
            current, timeout=(5, 20), stream=True, allow_redirects=False
        ) as response:
            if response.is_redirect:
                current = urljoin(current, response.headers["Location"])
                continue
            if response.status_code >= 400:
                raise status_error(response.status_code)
            content_type = response.headers.get("Content-Type", "")
            if require_html and "html" not in content_type.lower():
                raise ScrapeError("That URL did not return an HTML product page.")
            return current, read_capped(response), response.headers
    raise ScrapeError("The site redirected too many times.", 422)


def fetch_flipkart_page(url: str) -> str:
    """Return the HTML of one Flipkart reviews page."""
    if not SCRAPEDO_TOKEN:
        try:
            _, body, headers = safe_get(url, host_ok=is_flipkart_host)
        except ScrapeError as exc:
            if exc.http_status:
                raise ScrapeError(
                    f"{exc} For Flipkart, set SCRAPEDO_TOKEN (see README) to fetch "
                    "pages through a scraping service.",
                    exc.status,
                    exc.http_status,
                ) from exc
            raise
        return decode_body(body, headers)

    # Flipkart may populate the review state only after JavaScript runs. Ask
    # Scrape.do to render the page before returning its HTML.
    params = {
        "token": SCRAPEDO_TOKEN,
        "url": url,
        "geoCode": "in",
        "render": "true",
        "customWait": "2000",
    }
    if SCRAPEDO_SUPER:
        params["super"] = "true"
    with requests.get(
        "https://api.scrape.do/", params=params, timeout=(10, 60), stream=True
    ) as response:
        if response.status_code == 401:
            raise ScrapeError("Scrape.do rejected the token. Check SCRAPEDO_TOKEN.", 422)
        if response.status_code == 429:
            raise ScrapeError(
                "Scrape.do's request limit was reached. Try again later or check your plan.",
                422,
            )
        if response.status_code >= 400:
            raise ScrapeError(
                f"Scrape.do returned HTTP {response.status_code}. This can mean an "
                "invalid token, no credits left, or that Flipkart blocked the fetch "
                "(try SCRAPEDO_SUPER=1).",
                422,
                response.status_code,
            )
        return decode_body(read_capped(response), response.headers)


def dump_debug(html: str, label: str) -> None:
    """Save raw HTML when parsing fails, so the real page structure can be inspected."""
    if not DEBUG_DUMP_DIR:
        return
    try:
        directory = Path(DEBUG_DUMP_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{label}-{int(time.time())}.html").write_text(html, encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- parsing helpers


def _number(value):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def rating_value(value, best: float = 5.0):
    """Return a rating normalised to a 0-5 scale, or None."""
    if isinstance(value, dict):
        best = _number(value.get("bestRating")) or best
        raw = value.get("ratingValue")
        value = raw if raw is not None else value.get("value")
    text = str(value if value is not None else "").strip()
    if "/" in text:
        text, _, denominator = text.partition("/")
        best = _number(denominator) or best
    rating = _number(text)
    if rating is None or best <= 0 or not 0 <= rating <= best:
        return None
    return round(rating * 5 / best, 2)


def dig(obj, *keys, default=None):
    """Safely walk nested dicts/lists without raising on missing or null parts."""
    for key in keys:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return default
        if obj is None:
            return default
    return obj


def find_jsonld_reviews(data):
    found = []
    if isinstance(data, list):
        for item in data:
            found.extend(find_jsonld_reviews(item))
    elif isinstance(data, dict):
        review_type = data.get("@type", [])
        if isinstance(review_type, str):
            review_type = [review_type]
        if "Review" in review_type:
            text = data.get("reviewBody") or data.get("description") or data.get("name")
            if text:
                author = data.get("author")
                found.append(
                    {
                        "review": str(text).strip(),
                        "rating": rating_value(data.get("reviewRating")),
                        "date": str(data.get("datePublished") or ""),
                        "author": str(
                            author.get("name", "") if isinstance(author, dict) else author or ""
                        ),
                    }
                )
        for value in data.values():
            if isinstance(value, (dict, list)):
                found.extend(find_jsonld_reviews(value))
    return found


def text_of(node):
    return " ".join(node.stripped_strings)


OUT_OF = re.compile(r"(\d+(?:\.\d+)?)\s*(?:out of|/)\s*(\d+(?:\.\d+)?)", re.I)


def rating_from_node(node):
    """Read a star rating from explicit markup only (never from a count like '1,234 ratings')."""
    element = node.select_one('[itemprop="ratingValue"]')
    if element:
        rating = rating_value(element.get("content") or text_of(element))
        if rating is not None:
            return rating
    for element in node.select("[aria-label]"):
        match = OUT_OF.search(element.get("aria-label", ""))
        if match:
            rating = rating_value(f"{match.group(1)}/{match.group(2)}")
            if rating is not None:
                return rating
    return None


def outermost_review_nodes(soup):
    """Find review containers, dropping list wrappers and nested duplicates."""
    selectors = [
        '[itemprop="review"]',
        '[data-hook="review"]',
        '[class*="review-item"]',
        '[class*="review-card"]',
    ]
    nodes, seen = [], set()
    for selector in selectors:
        for node in soup.select(selector):
            if id(node) not in seen:
                nodes.append(node)
                seen.add(id(node))

    # A node containing two or more other matches is a wrapper around a list of reviews.
    single = [
        node
        for node in nodes
        if sum(id(child) in seen for child in node.find_all(True)) < 2
    ]
    single_ids = {id(node) for node in single}
    # Keep the outer node when one match sits inside another.
    return [
        node for node in single if not any(id(parent) in single_ids for parent in node.parents)
    ]


# --------------------------------------------------------------------------- Flipkart


def parse_flipkart_state(html: str):
    marker = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if not marker:
        return {}
    try:
        state, _ = json.JSONDecoder().raw_decode(html, marker.end())
    except json.JSONDecodeError:
        return {}
    return state if isinstance(state, dict) else {}


def flipkart_review_components(state):
    slots = dig(state, "multiWidgetState", "widgetsData", "slots", default=[])
    if not isinstance(slots, list):
        return []
    values = []
    for slot in slots:
        widget = dig(slot, "slotData", "widget", default={})
        if not isinstance(widget, dict) or widget.get("type") != "REVIEWS":
            continue
        components = dig(widget, "data", "renderableComponents", default=[])
        if not isinstance(components, list):
            continue
        for component in components:
            value = component.get("value") if isinstance(component, dict) else None
            if isinstance(value, dict):
                values.append(value)
    return values


def explain_flipkart_miss(html: str, state) -> str:
    lowered = html.lower()
    if any(marker in lowered for marker in BLOCK_MARKERS):
        message = "Flipkart returned a bot-check page instead of the reviews."
    elif not state:
        message = (
            "The response had no Flipkart page data. The request was probably "
            "blocked, or the page layout changed."
        )
    else:
        message = (
            "Flipkart page data was found but had no reviews widget. The product may "
            "have no reviews, or the page layout changed."
        )
    if not SCRAPEDO_TOKEN:
        message += " Set SCRAPEDO_TOKEN (see README) to fetch through Scrape.do."
    elif not SCRAPEDO_SUPER:
        message += " Try SCRAPEDO_SUPER=1."
    if not DEBUG_DUMP_DIR:
        message += " Set SCRAPE_DEBUG_DIR to save the raw page for inspection."
    return message


def scrape_flipkart_reviews(url: str):
    parsed = urlparse(url)
    if not is_flipkart_host(parsed.hostname):
        raise ValueError("Enter a Flipkart product or reviews page URL.")

    path = parsed.path
    if "/product-reviews/" not in path:
        if "/p/" not in path:
            raise ValueError("Use the Flipkart product page or its All Reviews page link.")
        path = path.replace("/p/", "/product-reviews/", 1)

    base_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"page", "sortOrder"}
    ]
    reviews, seen = [], set()
    requests_made = 0

    for sort_order, max_pages in FLIPKART_PLAN:
        for page_number in range(1, max_pages + 1):
            page_query = base_query + [("sortOrder", sort_order), ("page", str(page_number))]
            page_url = urlunparse(
                parsed._replace(path=path, query=urlencode(page_query), fragment="")
            )
            if requests_made and not SCRAPEDO_TOKEN:
                time.sleep(random.uniform(1.0, 2.5))  # be polite on direct requests
            html = fetch_flipkart_page(page_url)
            requests_made += 1

            state = parse_flipkart_state(html)
            components = flipkart_review_components(state)
            if not components:
                dump_debug(html, f"flipkart-{sort_order.lower()}-p{page_number}")
                if not reviews:
                    raise ScrapeError(explain_flipkart_miss(html, state), 422)
                break

            added = 0
            for item in components:
                review_text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
                title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
                combined = ": ".join(part for part in (title, review_text) if part)
                review_id = item.get("id") or combined.casefold()
                if not combined or review_id in seen:
                    continue
                seen.add(review_id)
                added += 1
                reviews.append(
                    {
                        "review": combined,
                        "rating": rating_value(item.get("rating")),
                        "date": str(item.get("created") or ""),
                        "author": "",
                    }
                )
                if len(reviews) >= MAX_REVIEWS:
                    return reviews
            if added == 0:
                break  # nothing new on this page: stop paging this sort order

    return reviews


# --------------------------------------------------------------------------- generic sites


def scrape_reviews(url: str):
    _, body, _ = safe_get(url, require_html=True)
    soup = BeautifulSoup(body, "html.parser")

    reviews = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            reviews.extend(
                find_jsonld_reviews(json.loads(script.string or script.get_text(), strict=False))
            )
        except (json.JSONDecodeError, TypeError):
            continue

    if not reviews:
        for node in outermost_review_nodes(soup):
            body_node = node.select_one(
                '[itemprop="reviewBody"], [data-hook="review-body"], '
                '[class*="review-text"], [class*="review-content"]'
            )
            review_text = text_of(body_node or node)
            if len(review_text) < 8:
                continue
            date_node = node.select_one("[itemprop='datePublished'], time")
            author_node = node.select_one("[itemprop='author'], [class*='author']")
            reviews.append(
                {
                    "review": review_text,
                    "rating": rating_from_node(node),
                    "date": (date_node.get("datetime") or text_of(date_node)) if date_node else "",
                    "author": text_of(author_node) if author_node else "",
                }
            )

    unique, seen_text = [], set()
    for review in reviews:
        text = re.sub(r"\s+", " ", review["review"]).strip()
        key = text.casefold()
        if text and key not in seen_text:
            review["review"] = text
            unique.append(review)
            seen_text.add(key)
        if len(unique) >= MAX_REVIEWS:
            break
    if not unique:
        raise ScrapeError(
            "No reviews were found on this page. The site may load reviews with JavaScript "
            "or use a layout that needs a dedicated scraper.",
            422,
        )
    return unique


# --------------------------------------------------------------------------- analysis

STOP_WORDS = frozenset(
    """
    about after again also always because been before being both could does doing done
    down each even ever every from further gets give going have having here into just
    like make many more most much must never only other over really same should since
    some still such take than that their them then there these they this those through
    under until very were what when where which while will with would your yours itself
    product review item items ordered bought purchase purchased flipkart amazon thing
    things thanks thank well
    """.split()
)


def rating_label(rating):
    if rating is None:
        return None
    return "Positive" if rating >= 4 else "Negative" if rating <= 2 else "Neutral"


def analyze(reviews):
    if not reviews:
        raise ScrapeError("No reviews were found to analyze.", 422)

    reviews_with_word = Counter()  # in how many reviews each word appears
    result = []
    short_review_count = 0
    rating_mismatch_count = 0
    for item in reviews:
        compound = sentiment_analyzer.polarity_scores(item["review"])["compound"]
        label = "Positive" if compound >= 0.05 else "Negative" if compound <= -0.05 else "Neutral"
        checks = []
        if len(item["review"].strip()) < 20:
            checks.append("Short text")
            short_review_count += 1
        rating_sentiment = rating_label(item["rating"])
        if (rating_sentiment == "Positive" and label == "Negative") or (
            rating_sentiment == "Negative" and label == "Positive"
        ):
            checks.append("Rating and text differ")
            rating_mismatch_count += 1
        result.append(
            {
                **item,
                "sentiment": label,
                "rating_sentiment": rating_sentiment,
                "checks": checks,
            }
        )
        words = {w.lower() for w in re.findall(r"[a-zA-Z]{4,}", item["review"])}
        reviews_with_word.update(words - STOP_WORDS)

    total = len(result)
    ratings = [item["rating"] for item in result if item["rating"] is not None]
    positive_count = sum(item["sentiment"] == "Positive" for item in result)
    rating_positive = sum(r >= 4 for r in ratings)
    top_words = reviews_with_word.most_common(5)
    theme = " ".join(word for word, _ in top_words[:2]).title() if top_words else "Not available"

    return {
        "reviews": result,
        "total_reviews": total,
        "positive_count": positive_count,
        "positive_percentage": round(100 * positive_count / total, 1),
        "rated_reviews": len(ratings),
        "rating_positive_percentage": (
            round(100 * rating_positive / len(ratings), 1) if ratings else None
        ),
        "average_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
        "common_theme": theme,
        "top_words": [{"word": word, "reviews": count} for word, count in top_words],
        "review_checks": {
            "short_text_count": short_review_count,
            "rating_text_mismatch_count": rating_mismatch_count,
        },
        "sample_warning": (
            f"Only {total} review(s) were available, so these percentages are a rough "
            "indication, not a reliable summary."
            if total < MIN_RELIABLE_SAMPLE
            else None
        ),
    }


# --------------------------------------------------------------------------- cache

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[0] < CACHE_TTL_SECONDS:
            return entry[1]
        _cache.pop(key, None)
    return None


def cache_put(key: str, value: dict) -> None:
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.time(), value)


# --------------------------------------------------------------------------- routes


@app.get("/")
def home():
    return app.send_static_file("index.html")


@app.post("/api/analyze")
def analyze_product():
    payload = request.get_json(silent=True) or {}
    raw_url = payload.get("url", "")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return jsonify({"error": "Enter a product page URL."}), 400

    try:
        url = validate_public_url(raw_url.strip())
        cache_key = url.split("#", 1)[0]
        if not payload.get("refresh"):
            cached = cache_get(cache_key)
            if cached:
                return jsonify({**cached, "cached": True})

        if is_flipkart_host(urlparse(url).hostname):
            reviews = scrape_flipkart_reviews(url)
        else:
            reviews = scrape_reviews(url)
        result = analyze(reviews)
        cache_put(cache_key, result)
        return jsonify(result)
    except ScrapeError as exc:
        return jsonify({"error": str(exc)}), exc.status
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.Timeout:
        return jsonify({"error": "The site took too long to respond. Try again."}), 422
    except requests.RequestException as exc:
        app.logger.warning("Fetch failed: %s: %s", type(exc).__name__, redact(str(exc)))
        return jsonify({"error": "Could not load that page. Check the link and try again."}), 422


@app.errorhandler(Exception)
def handle_unexpected(exc):
    # Always answer with JSON so the front end's response.json() never chokes on an HTML 500.
    if isinstance(exc, HTTPException):
        return exc
    app.logger.error("Unhandled error:\n%s", redact(traceback.format_exc()))
    return jsonify({"error": "Something went wrong while analyzing that page."}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
