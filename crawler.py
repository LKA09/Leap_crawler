from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import firebase_admin
import requests
from bs4 import BeautifulSoup, Tag
from firebase_admin import credentials, firestore
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "LeapCrawler/1.0 (+https://github.com/LKA09/Leap_crawler)"
DEFAULT_CONFIG_PATH = Path("config/sources.json")
DEFAULT_COLLECTION = "crawler_items"
RUN_COLLECTION = "crawler_runs"
MAX_ITEMS_HARD_LIMIT = 1000


@dataclass
class CrawlResult:
    source_key: str
    fetched: int
    created: int = 0
    updated: int = 0
    unchanged: int = 0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_sources() -> list[dict[str, Any]]:
    raw = os.getenv("CRAWLER_SOURCES_JSON")
    if raw:
        data = json.loads(raw)
    elif DEFAULT_CONFIG_PATH.exists():
        data = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        return []

    if isinstance(data, list):
        sources = data
    else:
        sources = data.get("sources", [])

    if not isinstance(sources, list):
        raise ValueError("sources must be a JSON array")
    return sources


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
        }
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def robots_allows(session: requests.Session, url: str, timeout: int) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = session.get(robots_url, timeout=timeout)
        if response.status_code >= 400:
            return True
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        # robots.txt 조회 자체가 실패했다고 대상 사이트 전체를 막지는 않는다.
        return True


def fetch_html(session: requests.Session, source: dict[str, Any]) -> str:
    url = required_string(source, "url")
    timeout = int(source.get("timeout_seconds", 20))
    if source.get("respect_robots_txt", True) and not robots_allows(session, url, timeout):
        raise RuntimeError(f"robots.txt disallows crawling: {url}")

    headers = source.get("headers") or {}
    response = session.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding
    return response.text


def required_string(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required string: {key}")
    return value.strip()


def extract_field(item: Tag, spec: dict[str, Any], base_url: str) -> Any:
    selector = spec.get("selector")
    node: Tag | None
    if selector:
        found = item.select_one(str(selector))
        node = found if isinstance(found, Tag) else None
    else:
        node = item

    if node is None:
        return None

    field_type = str(spec.get("type", "text"))
    if field_type == "text":
        value: Any = node.get_text(" ", strip=True)
    elif field_type == "attr":
        attr = spec.get("attr")
        if not attr:
            raise ValueError("attr field requires an attr name")
        value = node.get(str(attr))
        value = value.strip() if isinstance(value, str) else value
    else:
        raise ValueError(f"unsupported field type: {field_type}")

    if isinstance(value, str) and spec.get("absolute_url") and value:
        value = urljoin(base_url, value)

    return value


def parse_items(source: dict[str, Any], html: str) -> list[dict[str, Any]]:
    source_key = required_string(source, "key")
    base_url = required_string(source, "url")
    item_selector = required_string(source, "item_selector")
    fields = source.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError(f"{source_key}: fields must be a non-empty object")

    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select(item_selector)
    max_items = min(int(source.get("max_items", 200)), MAX_ITEMS_HARD_LIMIT)
    nodes = nodes[:max_items]

    if not nodes and not source.get("allow_empty", False):
        raise RuntimeError(f"{source_key}: item selector matched 0 elements: {item_selector}")

    parsed_items: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, Tag):
            continue
        parsed: dict[str, Any] = {}
        valid = True
        for field_name, raw_spec in fields.items():
            if not isinstance(raw_spec, dict):
                raise ValueError(f"{source_key}.{field_name}: field spec must be an object")
            value = extract_field(node, raw_spec, base_url)
            if raw_spec.get("required") and (value is None or value == ""):
                valid = False
                break
            if value is not None and value != "":
                parsed[str(field_name)] = value
        if valid and parsed:
            parsed_items.append(parsed)

    return parsed_items


def item_identity(item: dict[str, Any]) -> str:
    for key in ("id", "url", "title"):
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def item_doc_id(source_key: str, item: dict[str, Any]) -> str:
    identity = f"{source_key}:{item_identity(item)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]


def content_hash(item: dict[str, Any]) -> str:
    canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def init_firestore():
    if firebase_admin._apps:
        return firestore.client()

    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw)
        firebase_admin.initialize_app(credentials.Certificate(info))
    else:
        # 로컬에서는 GOOGLE_APPLICATION_CREDENTIALS 등 Application Default Credentials도 허용한다.
        firebase_admin.initialize_app()
    return firestore.client()


def commit_in_chunks(db, writes: list[tuple[Any, dict[str, Any]]], chunk_size: int = 450) -> None:
    for start in range(0, len(writes), chunk_size):
        batch = db.batch()
        for ref, payload in writes[start : start + chunk_size]:
            batch.set(ref, payload, merge=True)
        batch.commit()


def upsert_items(db, source: dict[str, Any], items: list[dict[str, Any]]) -> CrawlResult:
    source_key = required_string(source, "key")
    collection_name = str(source.get("collection", DEFAULT_COLLECTION)).strip() or DEFAULT_COLLECTION
    collection = db.collection(collection_name)

    refs = [collection.document(item_doc_id(source_key, item)) for item in items]
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
            "sourceKey": source_key,
            "sourcePage": source.get("url"),
            "contentHash": new_hash,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
        if old:
            updated += 1
        else:
            payload["createdAt"] = firestore.SERVER_TIMESTAMP
            created += 1
        writes.append((ref, payload))

    commit_in_chunks(db, writes)
    return CrawlResult(
        source_key=source_key,
        fetched=len(items),
        created=created,
        updated=updated,
        unchanged=unchanged,
    )


def save_run(db, source_key: str, status: str, **extra: Any) -> None:
    db.collection(RUN_COLLECTION).document(source_key).set(
        {
            "sourceKey": source_key,
            "status": status,
            "lastRunAt": firestore.SERVER_TIMESTAMP,
            **extra,
        },
        merge=True,
    )


def dry_run_print(source_key: str, items: list[dict[str, Any]]) -> None:
    print(f"[{source_key}] parsed {len(items)} items")
    print(json.dumps(items[:5], ensure_ascii=False, indent=2))
    if len(items) > 5:
        print(f"... {len(items) - 5} more items omitted")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generic HTML -> Firestore crawler for LEAP")
    parser.add_argument("--dry-run", action="store_true", help="parse and print without writing Firestore")
    parser.add_argument("--source", help="run only one source key")
    args = parser.parse_args()

    try:
        sources = load_sources()
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    sources = [source for source in sources if isinstance(source, dict) and source.get("enabled", True)]
    if args.source:
        sources = [source for source in sources if source.get("key") == args.source]

    if not sources:
        print("No enabled crawler sources. Set CRAWLER_SOURCES_JSON or create config/sources.json.")
        return 0

    session = build_session()
    db = None if args.dry_run else init_firestore()
    failed = 0

    for source in sources:
        source_key = str(source.get("key") or "unknown")
        started_at = utc_now_iso()
        try:
            html = fetch_html(session, source)
            items = parse_items(source, html)
            if args.dry_run:
                dry_run_print(source_key, items)
                continue

            result = upsert_items(db, source, items)
            save_run(
                db,
                source_key,
                "success",
                startedAt=started_at,
                fetched=result.fetched,
                created=result.created,
                updated=result.updated,
                unchanged=result.unchanged,
                error=None,
            )
            print(
                f"[{source_key}] fetched={result.fetched} created={result.created} "
                f"updated={result.updated} unchanged={result.unchanged}"
            )
        except Exception as exc:
            failed += 1
            print(f"[{source_key}] FAILED: {exc}", file=sys.stderr)
            if db is not None:
                try:
                    save_run(db, source_key, "failed", startedAt=started_at, error=str(exc)[:1500])
                except Exception as run_exc:
                    print(f"[{source_key}] could not save run status: {run_exc}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
