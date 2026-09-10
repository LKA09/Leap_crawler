from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

from firebase_admin import firestore
from playwright.async_api import Frame, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from crawler import commit_in_chunks, init_firestore

CONFIG_ENV = "NAVER_CRAWL_CONFIG_JSON"
LOCAL_CONFIG = Path("config/naver.json")
DEFAULT_COLLECTION = "crawler_items"
RUN_COLLECTION = "crawler_runs"
SOURCE_KEY = "naver-map"
MAX_QUERIES = 20
MAX_RESULTS_PER_QUERY = 30

PRICE_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)\s*원(?:\s*~)?$")
PHONE_RE = re.compile(r"(?:0\d{1,2}-\d{3,4}-\d{4}|\d{4}-\d{4})")
PLACE_ID_RE = re.compile(r"(?:/place/|/restaurant/|placeId=)(\d+)")
RATING_RE = re.compile(r"별점\s*([0-5](?:\.\d+)?)")
VISITOR_REVIEW_RE = re.compile(r"방문자\s*리뷰\s*([\d,]+)")
BLOG_REVIEW_RE = re.compile(r"블로그\s*리뷰\s*([\d,]+)")

BLOCK_TEXTS = (
    "자동입력 방지",
    "비정상적인 접근",
    "서비스 이용이 제한",
    "captcha",
)

CONTROL_LINES = {
    "홈",
    "메뉴",
    "리뷰",
    "사진",
    "지도",
    "주변",
    "정보",
    "알림받기",
    "저장",
    "길찾기",
    "거리뷰",
    "공유",
    "예약",
    "주문",
    "전화",
    "주소",
    "복사",
    "펼쳐보기",
    "접기",
}


def load_config(path: str | None) -> dict[str, Any]:
    raw = os.getenv(CONFIG_ENV)
    if raw:
        data = json.loads(raw)
    else:
        target = Path(path) if path else LOCAL_CONFIG
        if not target.exists():
            return {}
        data = json.loads(target.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError("Naver crawler config must be a JSON object")
    return data


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = clean_text(raw)
        if not line or line in lines:
            continue
        lines.append(line)
    return lines


def find_after_label(lines: list[str], labels: tuple[str, ...], max_lookahead: int = 2) -> str:
    for index, line in enumerate(lines):
        if line in labels or any(line.startswith(label + " ") for label in labels):
            inline = line
            for label in labels:
                if inline.startswith(label + " "):
                    candidate = clean_text(inline[len(label) :])
                    if candidate:
                        return candidate
            for offset in range(1, max_lookahead + 1):
                if index + offset >= len(lines):
                    break
                candidate = lines[index + offset]
                if candidate not in CONTROL_LINES:
                    return candidate
    return ""


def parse_menu_lines(text: str, limit: int = 40) -> list[dict[str, str]]:
    lines = clean_lines(text)
    menus: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for index, line in enumerate(lines):
        if not PRICE_RE.match(line):
            continue

        name = ""
        for prev in range(index - 1, max(-1, index - 5), -1):
            candidate = lines[prev]
            if candidate in CONTROL_LINES or PRICE_RE.match(candidate):
                continue
            if any(token in candidate for token in ("리뷰", "사진", "원산지", "대표", "주문")):
                continue
            name = candidate
            break

        if not name:
            continue

        pair = (name, line)
        if pair in seen:
            continue
        seen.add(pair)
        menus.append({"name": name[:120], "price": line[:60]})
        if len(menus) >= limit:
            break

    return menus


def parse_count(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def extract_place_id(url: str) -> str:
    match = PLACE_ID_RE.search(url)
    return match.group(1) if match else ""


def stable_doc_id(item: dict[str, Any]) -> str:
    identity = item.get("naverPlaceId") or item.get("url") or item.get("title")
    raw = f"{SOURCE_KEY}:{identity}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def content_hash(item: dict[str, Any]) -> str:
    stable = {key: value for key, value in item.items() if key not in {"matchedQueries"}}
    canonical = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def wait_for_frame(
    page: Page,
    *,
    names: tuple[str, ...] = (),
    url_hints: tuple[str, ...] = (),
    timeout_ms: int = 12000,
) -> Frame | None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            if frame.name in names:
                return frame
            lowered = frame.url.lower()
            if any(hint.lower() in lowered for hint in url_hints):
                return frame
        await page.wait_for_timeout(200)
    return None


async def frame_body_text(frame: Frame) -> str:
    try:
        return await frame.locator("body").inner_text(timeout=5000)
    except PlaywrightTimeoutError:
        return ""


async def first_text(frame: Frame, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        locator = frame.locator(selector)
        try:
            count = min(await locator.count(), 5)
        except Exception:
            continue
        for index in range(count):
            try:
                text = clean_text(await locator.nth(index).inner_text(timeout=1000))
            except Exception:
                continue
            if text:
                return text
    return ""


async def collect_images(frame: Frame, limit: int = 5) -> list[str]:
    images: list[str] = []
    locator = frame.locator("img")
    try:
        count = min(await locator.count(), 80)
    except Exception:
        return images

    for index in range(count):
        try:
            src = clean_text(await locator.nth(index).get_attribute("src"))
        except Exception:
            continue
        if not src.startswith("https://"):
            continue
        if "pstatic.net" not in src and "naver.net" not in src:
            continue
        if src not in images:
            images.append(src)
        if len(images) >= limit:
            break
    return images


async def click_menu(frame: Frame) -> None:
    candidates = (
        frame.get_by_role("link", name="메뉴", exact=True),
        frame.get_by_role("button", name="메뉴", exact=True),
        frame.get_by_text("메뉴", exact=True),
    )
    for locator in candidates:
        try:
            if await locator.count() == 0:
                continue
            target = locator.first
            if not await target.is_visible():
                continue
            await target.click(timeout=3000)
            await frame.page.wait_for_timeout(1200)
            return
        except Exception:
            continue


async def discover_place_links(page: Page, query: str, max_results: int, timeout_ms: int) -> list[str]:
    search_url = f"https://map.naver.com/p/search/{quote(query, safe='')}"
    await page.goto(search_url, wait_until="domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(1800)

    search_frame = await wait_for_frame(
        page,
        names=("searchIframe",),
        url_hints=("/search", "search"),
        timeout_ms=min(timeout_ms, 12000),
    )

    # A very specific query can open the place detail directly.
    direct_entry = await wait_for_frame(
        page,
        names=("entryIframe",),
        url_hints=("/place/", "/restaurant/", "pcmap.place"),
        timeout_ms=1500,
    )
    if direct_entry and extract_place_id(direct_entry.url):
        return [direct_entry.url]

    if search_frame is None:
        return []

    # Scroll the result pane a few times so lazy-loaded links become available.
    for _ in range(3):
        try:
            await search_frame.locator("body").evaluate("el => el.scrollTo(0, el.scrollHeight)")
        except Exception:
            pass
        await page.wait_for_timeout(400)

    hrefs: list[str] = []
    anchors = search_frame.locator("a")
    try:
        count = min(await anchors.count(), 400)
    except Exception:
        count = 0

    for index in range(count):
        anchor = anchors.nth(index)
        try:
            href = clean_text(await anchor.get_attribute("href"))
        except Exception:
            continue
        if not href:
            continue
        absolute = urljoin("https://map.naver.com", href)
        if not extract_place_id(absolute):
            continue
        if absolute not in hrefs:
            hrefs.append(absolute)
        if len(hrefs) >= max_results:
            break

    return hrefs


async def extract_place(page: Page, place_url: str, query: str, include_menu: bool, timeout_ms: int) -> dict[str, Any] | None:
    if place_url.startswith("http") and "map.naver.com" in place_url:
        await page.goto(place_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1200)

    entry = await wait_for_frame(
        page,
        names=("entryIframe",),
        url_hints=("pcmap.place", "/place/", "/restaurant/"),
        timeout_ms=min(timeout_ms, 12000),
    )

    frame = entry or page.main_frame
    text = await frame_body_text(frame)
    lowered = text.lower()
    if any(block.lower() in lowered for block in BLOCK_TEXTS):
        raise RuntimeError("Naver displayed an access restriction/CAPTCHA; crawler stops instead of bypassing it")
    if not text:
        return None

    lines = clean_lines(text)
    frame_url = frame.url or place_url
    place_id = extract_place_id(frame_url) or extract_place_id(place_url)

    title = await first_text(
        frame,
        (
            "h1",
            "h2",
            "span.GHAhO",
            "span.Fc1rA",
            "[class*='place'] h1",
            "[class*='place'] h2",
        ),
    )
    if not title:
        for line in lines[:15]:
            if line not in CONTROL_LINES and not any(token in line for token in ("리뷰", "별점", "사진")):
                title = line
                break
    if not title:
        return None

    category = await first_text(frame, ("span.lnJFt", "[class*='category']"))
    address = await first_text(frame, ("span.LDgIH", "[class*='address']"))
    phone = await first_text(frame, ("span.xlx7Q", "a[href^='tel:']"))

    if not address:
        address = find_after_label(lines, ("주소", "도로명"), 3)
    if not phone:
        phone_match = PHONE_RE.search(text)
        phone = phone_match.group(0) if phone_match else ""

    rating_match = RATING_RE.search(text)
    rating = float(rating_match.group(1)) if rating_match else None
    visitor_reviews = parse_count(VISITOR_REVIEW_RE, text)
    blog_reviews = parse_count(BLOG_REVIEW_RE, text)

    images = await collect_images(frame)
    menus: list[dict[str, str]] = []
    if include_menu:
        await click_menu(frame)
        menu_text = await frame_body_text(frame)
        if menu_text:
            menus = parse_menu_lines(menu_text)

    canonical_url = place_url if "map.naver.com" in place_url else f"https://map.naver.com/p/entry/place/{place_id}" if place_id else place_url
    summary_parts = [part for part in (category, address) if part]

    item: dict[str, Any] = {
        "kind": "naver_restaurant",
        "title": title[:200],
        "url": canonical_url,
        "sourceQuery": query,
    }
    if place_id:
        item["naverPlaceId"] = place_id
    if category:
        item["category"] = category[:120]
    if address:
        item["address"] = address[:300]
    if phone:
        item["phone"] = phone[:60]
    if rating is not None:
        item["rating"] = rating
    if visitor_reviews is not None:
        item["visitorReviewCount"] = visitor_reviews
    if blog_reviews is not None:
        item["blogReviewCount"] = blog_reviews
    if images:
        item["imageUrl"] = images[0]
        item["imageUrls"] = images
    if menus:
        item["menus"] = menus
    if summary_parts:
        item["summary"] = " · ".join(summary_parts)[:500]
    return item


def merge_item(items: dict[str, dict[str, Any]], item: dict[str, Any], query: str) -> None:
    key = str(item.get("naverPlaceId") or item.get("url") or item.get("title"))
    existing = items.get(key)
    if existing is None:
        merged = dict(item)
        merged["matchedQueries"] = [query]
        items[key] = merged
        return

    queries = list(existing.get("matchedQueries") or [])
    if query not in queries:
        queries.append(query)
    existing["matchedQueries"] = queries

    # Prefer richer non-empty values from the latest successful detail parse.
    for field, value in item.items():
        if value not in (None, "", [], {}):
            existing[field] = value


def upsert_items(db, collection_name: str, items: list[dict[str, Any]]) -> dict[str, int]:
    collection = db.collection(collection_name)
    refs = [collection.document(stable_doc_id(item)) for item in items]
    existing = {snap.id: (snap.to_dict() or {}) for snap in db.get_all(refs)} if refs else {}

    writes: list[tuple[Any, dict[str, Any]]] = []
    created = updated = unchanged = 0

    for ref, item in zip(refs, items):
        new_hash = content_hash(item)
        old = existing.get(ref.id)
        if old and old.get("contentHash") == new_hash:
            unchanged += 1
            continue

        payload = {
            **item,
            "sourceKey": SOURCE_KEY,
            "sourcePage": "https://map.naver.com/",
            "contentHash": new_hash,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
        if old:
            updated += 1
        else:
            created += 1
            payload["createdAt"] = firestore.SERVER_TIMESTAMP
        writes.append((ref, payload))

    commit_in_chunks(db, writes)
    return {"created": created, "updated": updated, "unchanged": unchanged}


def save_run(db, status: str, **extra: Any) -> None:
    db.collection(RUN_COLLECTION).document(SOURCE_KEY).set(
        {
            "sourceKey": SOURCE_KEY,
            "status": status,
            "lastRunAt": firestore.SERVER_TIMESTAMP,
            **extra,
        },
        merge=True,
    )


async def run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    raw_queries = config.get("queries") or []
    if args.query:
        raw_queries = [args.query]
    queries = [clean_text(str(query)) for query in raw_queries if clean_text(str(query))]
    queries = queries[:MAX_QUERIES]
    if not queries:
        print(f"No Naver queries configured. Set {CONFIG_ENV} or create {LOCAL_CONFIG}.")
        return 0

    max_results = int(args.max_results or config.get("max_results_per_query", 8))
    max_results = max(1, min(max_results, MAX_RESULTS_PER_QUERY))
    include_menu = bool(config.get("include_menu", True))
    delay_ms = max(700, min(int(config.get("delay_ms", 1400)), 10000))
    timeout_ms = max(10000, min(int(config.get("navigation_timeout_ms", 30000)), 60000))
    collection_name = clean_text(str(config.get("collection", DEFAULT_COLLECTION))) or DEFAULT_COLLECTION
    headless = not args.headed

    aggregated: dict[str, dict[str, Any]] = {}
    failed_queries = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_timeout(10000)

        for query in queries:
            try:
                links = await discover_place_links(page, query, max_results, timeout_ms)
                if not links:
                    print(f"[{query}] no place links found; Naver DOM may have changed", file=sys.stderr)
                    failed_queries += 1
                    continue

                print(f"[{query}] discovered {len(links)} place(s)")
                for index, link in enumerate(links, 1):
                    try:
                        item = await extract_place(page, link, query, include_menu, timeout_ms)
                        if item:
                            merge_item(aggregated, item, query)
                            print(f"[{query}] {index}/{len(links)} {item.get('title')}")
                    except Exception as exc:
                        print(f"[{query}] place failed: {link}: {exc}", file=sys.stderr)
                    await page.wait_for_timeout(delay_ms)
            except Exception as exc:
                failed_queries += 1
                print(f"[{query}] FAILED: {exc}", file=sys.stderr)

        await context.close()
        await browser.close()

    items = list(aggregated.values())
    if args.dry_run:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 1 if failed_queries and not items else 0

    db = init_firestore()
    try:
        stats = upsert_items(db, collection_name, items)
        save_run(
            db,
            "success" if failed_queries == 0 else "partial",
            queries=queries,
            failedQueries=failed_queries,
            fetched=len(items),
            **stats,
            error=None,
        )
        print(
            f"[naver-map] fetched={len(items)} created={stats['created']} "
            f"updated={stats['updated']} unchanged={stats['unchanged']} failedQueries={failed_queries}"
        )
    except Exception as exc:
        print(f"Firestore sync failed: {exc}", file=sys.stderr)
        try:
            save_run(db, "failed", queries=queries, fetched=len(items), error=str(exc)[:1500])
        except Exception:
            pass
        return 1

    return 1 if failed_queries and not items else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Naver Maps restaurant -> Firestore crawler for LEAP")
    parser.add_argument("--config", help="local JSON config path (default: config/naver.json)")
    parser.add_argument("--query", help="override config and crawl one query")
    parser.add_argument("--max-results", type=int, help="override max results per query")
    parser.add_argument("--dry-run", action="store_true", help="crawl and print JSON without Firestore writes")
    parser.add_argument("--headed", action="store_true", help="show Chromium window for local debugging")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
