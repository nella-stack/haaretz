#!/usr/bin/env python3
"""
Download Haaretz logic puzzles (tashbets) for a given date range.

Usage:
    python download_puzzles.py --from 2025-12-01 --to 2026-02-15 --cookies cookies.txt

The cookies file should contain your Haaretz session cookies.
You can export them from your browser using a cookie export extension
(e.g., "Get cookies.txt LOCALLY" for Chrome) in Netscape/Mozilla cookie format.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from http.cookiejar import MozillaCookieJar
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.haaretz.co.il"
SECTION_URL = f"{BASE_URL}/magazine/haaretzlogicpuzzle"
IMAGE_HOST = "https://img.haarets.co.il"

# Known non-puzzle images that appear on every page (logos, section headers)
SKIP_IMAGE_FILENAMES = {"htzmobile.png", "htzdesktop.png"}
# This is the generic section header image, not an actual puzzle
SKIP_IMAGE_IDS = {"0000017f-db24-d3a5-af7f-fbae700d0000"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
}


def load_cookies(session: requests.Session, cookies_path: str) -> None:
    """Load cookies from a Netscape-format cookies.txt file or JSON file."""
    if cookies_path.endswith(".json"):
        with open(cookies_path) as f:
            cookies = json.load(f)
        for cookie in cookies:
            session.cookies.set(
                cookie.get("name", cookie.get("Name")),
                cookie.get("value", cookie.get("Value")),
                domain=cookie.get("domain", cookie.get("Domain", ".haaretz.co.il")),
            )
    else:
        cookie_jar = MozillaCookieJar(cookies_path)
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies = cookie_jar


def extract_date_from_url(url: str) -> datetime | None:
    """Extract the date from a puzzle article URL."""
    match = re.search(r"/(\d{4}-\d{2}-\d{2})/", url)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d")
    return None


def find_puzzle_articles(session: requests.Session, max_pages: int = 20) -> list[dict]:
    """
    Scrape the puzzle section listing page(s) to find all article URLs.
    Returns a list of dicts: {"url": str, "date": datetime, "title": str}
    """
    articles = []
    seen_urls = set()

    for page_num in range(1, max_pages + 1):
        url = SECTION_URL if page_num == 1 else f"{SECTION_URL}?page={page_num}"
        print(f"Fetching listing page {page_num}...")

        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  Error fetching page {page_num}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        # Find all links that match the puzzle article URL pattern
        new_count = 0
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/magazine/haaretzlogicpuzzle/" not in href:
                continue
            if "/ty-article" not in href:
                continue

            full_url = urljoin(BASE_URL, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            article_date = extract_date_from_url(full_url)
            if not article_date:
                continue

            # Try to get the title from the link text or parent elements
            title = link.get_text(strip=True) or ""

            articles.append({
                "url": full_url,
                "date": article_date,
                "title": title,
            })
            new_count += 1

        print(f"  Found {new_count} new article(s) on page {page_num}")

        if new_count == 0:
            print("  No new articles found, stopping pagination.")
            break

        time.sleep(1)  # Be polite

    articles.sort(key=lambda a: a["date"])
    return articles


def is_skip_image(src: str) -> bool:
    """Check if an image URL is a known non-puzzle image (logo, header, etc.)."""
    filename = src.split("?")[0].split("/")[-1]
    if filename in SKIP_IMAGE_FILENAMES:
        return True
    for skip_id in SKIP_IMAGE_IDS:
        if skip_id in src:
            return True
    return False


def get_image_context(img_tag) -> str:
    """
    Get the textual context around an image tag.
    Looks at alt text, parent figure caption, and nearby headings/text.
    """
    parts = []

    # Alt text
    alt = img_tag.get("alt", "")
    if alt:
        parts.append(alt)

    # Check parent <figure> for <figcaption>
    figure = img_tag.find_parent("figure")
    if figure:
        caption = figure.find("figcaption")
        if caption:
            parts.append(caption.get_text(strip=True))

    # Walk up to find the nearest heading or bold text before this image
    for parent in img_tag.parents:
        # Look for a heading before this element
        prev = parent.find_previous_sibling(re.compile(r"h[1-6]|strong|b"))
        if prev:
            parts.append(prev.get_text(strip=True))
            break
        # Also check previous siblings for any text containing puzzle keywords
        prev_sib = parent.find_previous_sibling()
        if prev_sib:
            text = prev_sib.get_text(strip=True)
            if text and len(text) < 200:
                parts.append(text)
            break

    return " ".join(parts)


def extract_puzzle_images(
    session: requests.Session,
    article_url: str,
    filter_keyword: str = "",
    verbose: bool = False,
) -> list[str]:
    """
    Fetch an article page and extract puzzle image URLs from the article body.
    If filter_keyword is set, only return images whose surrounding text matches.
    Returns a list of image URLs.
    """
    try:
        resp = session.get(article_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Error fetching article: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Collect all candidate images with their context
    candidates = []  # list of (url, context_text)

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src or IMAGE_HOST not in src:
            continue
        if is_skip_image(src):
            continue
        # Skip small thumbnails
        width_match = re.search(r"width=(\d+)", src)
        if width_match and int(width_match.group(1)) < 400:
            continue

        context = get_image_context(img)
        candidates.append((src, context))
        if verbose:
            print(f"  [candidate] context='{context[:80]}' url={src}")

    # Filter by keyword if specified
    if filter_keyword and candidates:
        filtered = [
            (url, ctx) for url, ctx in candidates
            if filter_keyword in ctx
        ]
        if filtered:
            candidates = filtered
            if verbose:
                print(f"  [filter] Matched {len(filtered)} image(s) for '{filter_keyword}'")
        else:
            if verbose:
                print(f"  [filter] No images matched '{filter_keyword}', using all candidates")

    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for url, _ctx in candidates:
        base = url.split("?")[0]
        if base not in seen:
            seen.add(base)
            unique_urls.append(url)

    if verbose and not unique_urls:
        all_imgs = [
            img.get("src", "")
            for img in soup.find_all("img")
            if IMAGE_HOST in img.get("src", "")
        ]
        print(f"  [debug] All img.haarets.co.il images on page: {len(all_imgs)}")
        for img_src in all_imgs:
            print(f"    {img_src}")

    return unique_urls


def get_full_res_url(image_url: str) -> str:
    """Replace width/height in image URL with full resolution values."""
    url = re.sub(r"width=\d+", "width=1180", image_url)
    url = re.sub(r"height=\d+", "height=1557", url)
    return url


def download_image(
    session: requests.Session,
    image_url: str,
    output_path: str,
) -> bool:
    """Download an image at full resolution."""
    full_res = get_full_res_url(image_url)
    try:
        resp = session.get(full_res, headers=HEADERS, timeout=60, stream=True)
        resp.raise_for_status()

        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) < 1000:
            print("  WARNING: Image is suspiciously small, may be a placeholder.")

        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except requests.RequestException as e:
        print(f"  Error downloading image: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Download Haaretz logic puzzles for a date range.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --from 2025-12-01 --to 2026-02-15 --cookies cookies.txt
  %(prog)s --from 2026-01-01 --to 2026-02-01 --cookies cookies.json --output ./puzzles

Cookie file:
  Export your Haaretz session cookies from your browser using an extension
  like "Get cookies.txt LOCALLY" (Netscape format) or export as JSON.
        """,
    )
    parser.add_argument(
        "--from", dest="from_date", required=True,
        help="Start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--to", dest="to_date", required=True,
        help="End date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--cookies", required=True,
        help="Path to cookies file (Netscape .txt or .json format)",
    )
    parser.add_argument(
        "--output", default="./puzzles",
        help="Output directory for downloaded puzzles (default: ./puzzles)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=20,
        help="Maximum number of listing pages to scan (default: 20)",
    )
    parser.add_argument(
        "--filter", default="3תשבץ",
        help="Only download images matching this keyword in nearby text "
             "(default: '3תשבץ' = crossword #3 only). "
             "Use '1תשבץ' for #1, 'תשבץ' for all crosswords, '' for all images.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show detailed debug output for image extraction",
    )

    args = parser.parse_args()

    try:
        from_date = datetime.strptime(args.from_date, "%Y-%m-%d")
        to_date = datetime.strptime(args.to_date, "%Y-%m-%d")
    except ValueError:
        print("Error: Dates must be in YYYY-MM-DD format.")
        sys.exit(1)

    if from_date > to_date:
        print("Error: --from date must be before --to date.")
        sys.exit(1)

    if not os.path.isfile(args.cookies):
        print(f"Error: Cookies file not found: {args.cookies}")
        sys.exit(1)

    os.makedirs(args.output, exist_ok=True)

    # Set up session with cookies
    session = requests.Session()
    load_cookies(session, args.cookies)

    print(f"Searching for puzzles from {from_date.date()} to {to_date.date()}...\n")

    # Find all articles from the listing page(s)
    all_articles = find_puzzle_articles(session, max_pages=args.max_pages)
    print(f"\nFound {len(all_articles)} total puzzle article(s).\n")

    # Filter to requested date range
    matching = [
        a for a in all_articles
        if from_date <= a["date"] <= to_date
    ]
    print(f"{len(matching)} puzzle(s) in the requested date range.\n")

    if not matching:
        print("No puzzles found in the specified date range.")
        print("Puzzles are published weekly (Thursdays). Check your date range.")
        sys.exit(0)

    # Download each puzzle
    downloaded = 0
    downloaded_hashes = {}  # md5 -> first filename, for duplicate detection
    for article in matching:
        date_str = article["date"].strftime("%Y-%m-%d")
        print(f"Processing puzzle for {date_str}...")
        print(f"  URL: {article['url']}")

        images = extract_puzzle_images(
            session, article["url"],
            filter_keyword=args.filter,
            verbose=args.verbose,
        )
        if not images:
            print("  WARNING: No puzzle images found on this page.")
            print("  (This may be a paywall issue - check your cookies.)")
            continue

        for i, img_url in enumerate(images):
            ext = "jpg"
            url_path = img_url.split("?")[0]
            if "." in url_path.split("/")[-1]:
                ext = url_path.split("/")[-1].rsplit(".", 1)[-1].lower()

            suffix = f"_{i+1}" if len(images) > 1 else ""
            filename = f"tashbetz_{date_str}{suffix}.{ext}"
            output_path = os.path.join(args.output, filename)

            print(f"  Downloading image to {output_path}...")
            if download_image(session, img_url, output_path):
                print(f"  Saved: {filename}")
                downloaded += 1

                # Check for duplicates (sign of paywall blocking)
                with open(output_path, "rb") as f:
                    file_hash = hashlib.md5(f.read()).hexdigest()
                if file_hash in downloaded_hashes:
                    print(
                        f"  WARNING: This image is identical to "
                        f"{downloaded_hashes[file_hash]}. "
                        f"Your cookies may have expired."
                    )
                else:
                    downloaded_hashes[file_hash] = filename

        time.sleep(1)  # Be polite between requests

    print(f"\nDone! Downloaded {downloaded} image(s) to {args.output}/")

    if len(downloaded_hashes) == 1 and downloaded > 1:
        print(
            "\nWARNING: All downloaded images appear identical! "
            "This usually means the paywall is blocking access. "
            "Please check that your cookies are valid and not expired."
        )


if __name__ == "__main__":
    main()
