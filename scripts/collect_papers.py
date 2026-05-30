#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import difflib
import email.utils
import html
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ARXIV_API_URL = "https://export.arxiv.org/api/query"
DBLP_API_URL = os.getenv("DBLP_API_URL", "http://dblp.org/search/publ/api")
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
CROSSREF_WORKS_URL = "https://api.crossref.org/works"
SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
DEFAULT_CONFIG = Path("config/interests.json")
DEFAULT_OUTPUT = Path("web/data/papers.json")
DEFAULT_CONFERENCE_OUTPUT = Path("web/data/conference_papers.json")
RETAINED_MATCH_LEVELS = {"high", "medium"}
DEFAULT_MAX_NEW_PAPERS = 50
DEFAULT_MAX_STORED_PAPERS = 50
DEFAULT_MAX_NEW_CONFERENCE_PAPERS = 50
DEFAULT_MAX_STORED_CONFERENCE_PAPERS = 300
DEFAULT_MAX_DATA_BYTES = 8 * 1024 * 1024
DEFAULT_RECENT_HISTORY_DAYS = 45
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
DBLP_TRANSIENT_HTTP_CODES = {429, 502, 503, 504}
DEFAULT_SOURCE_TYPES = ["arxiv", "openalex", "crossref"]
FEED_NAMESPACES = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass(frozen=True)
class Topic:
    id: str
    name: str
    description: str
    keywords: list[str]
    arxiv_categories: list[str]


@dataclass(frozen=True)
class SourceConfig:
    type: str
    name: str
    url: str = ""
    enabled: bool = True
    headers_env: str = ""
    bearer_token_env: str = ""


@dataclass(frozen=True)
class ConferenceSource:
    id: str
    name: str
    group: str
    dblp_toc_patterns: list[str]
    years: list[int]
    enabled: bool = True


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def json_size_bytes(data: dict[str, Any]) -> int:
    return len(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")) + 1


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


def env_list(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name, "")
    if not value.strip():
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_topics(config: dict[str, Any]) -> list[Topic]:
    topics = []
    for item in config.get("topics", []):
        topic_id = item.get("id") or slugify(item.get("name", "topic"))
        topics.append(
            Topic(
                id=topic_id,
                name=item["name"],
                description=item.get("description", ""),
                keywords=[str(k) for k in item.get("keywords", [])],
                arxiv_categories=[str(c) for c in item.get("arxiv_categories", [])],
            )
        )
    if not topics:
        raise ValueError("No topics found in configuration.")
    return topics


def parse_sources(config: dict[str, Any]) -> list[SourceConfig]:
    configured = config.get("sources")
    if not configured:
        configured = [{"type": source_type} for source_type in env_list("PAPER_SOURCES", DEFAULT_SOURCE_TYPES)]

    sources = []
    for item in configured:
        if isinstance(item, str):
            item = {"type": item}
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("type") or "").strip().lower()
        if not source_type:
            continue
        if (
            source_type in {"semantic_scholar", "semanticscholar", "semantic-scholar"}
            and not env_flag("ENABLE_SEMANTIC_SCHOLAR", False)
        ):
            continue
        if item.get("enabled", True) is False:
            continue
        name = str(item.get("name") or source_type.replace("_", " ").title())
        sources.append(
            SourceConfig(
                type=source_type,
                name=name,
                url=str(item.get("url") or ""),
                enabled=bool(item.get("enabled", True)),
                headers_env=str(item.get("headers_env") or ""),
                bearer_token_env=str(item.get("bearer_token_env") or ""),
            )
        )
    return sources


def merge_venues(default_venues: list[dict[str, Any]], override_venues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for venue in [*default_venues, *override_venues]:
        if not isinstance(venue, dict):
            continue
        venue_id = str(venue.get("id") or slugify(str(venue.get("name", "venue"))))
        if venue_id not in by_id:
            order.append(venue_id)
            by_id[venue_id] = {"id": venue_id}
        by_id[venue_id].update(venue)
        by_id[venue_id]["id"] = venue_id
    return [by_id[venue_id] for venue_id in order]


def merge_config(default_config: dict[str, Any], override_config: dict[str, Any] | None) -> dict[str, Any]:
    if not override_config:
        return default_config

    merged = copy.deepcopy(default_config)
    for key, value in override_config.items():
        if key == "conference_sources" and isinstance(value, dict):
            default_sources = merged.get("conference_sources", {})
            if not isinstance(default_sources, dict):
                default_sources = {}
            merged_sources = copy.deepcopy(default_sources)
            include_defaults = bool(value.get("include_default_venues", True))
            default_venues = default_sources.get("venues", []) if include_defaults else []
            override_venues = value.get("venues", [])
            additional_venues = value.get("additional_venues", [])
            for source_key, source_value in value.items():
                if source_key not in {"venues", "additional_venues", "include_default_venues"}:
                    merged_sources[source_key] = source_value
            if "venues" in value or "additional_venues" in value or not include_defaults:
                merged_sources["venues"] = merge_venues(
                    default_venues if isinstance(default_venues, list) else [],
                    [
                        *(override_venues if isinstance(override_venues, list) else []),
                        *(additional_venues if isinstance(additional_venues, list) else []),
                    ],
                )
            merged["conference_sources"] = merged_sources
        else:
            merged[key] = value
    return merged


def parse_years(value: Any) -> list[int]:
    years = []
    if not isinstance(value, list):
        return years
    for item in value:
        try:
            years.append(int(item))
        except (TypeError, ValueError):
            continue
    return sorted(set(years), reverse=True)


def default_conference_years(config: dict[str, Any], now: dt.datetime) -> list[int]:
    configured = parse_years(config.get("years"))
    if configured:
        return configured
    current_year = int(config.get("current_year") or now.year)
    lookback_years = max(1, int(config.get("lookback_years", 2) or 2))
    return [current_year - offset for offset in range(lookback_years)]


def parse_conference_sources(config: dict[str, Any], now: dt.datetime) -> list[ConferenceSource]:
    source_config = config.get("conference_sources", {})
    if not isinstance(source_config, dict) or not source_config.get("enabled", False):
        return []

    default_years = default_conference_years(source_config, now)
    sources = []
    for item in source_config.get("venues", []):
        if not isinstance(item, dict):
            continue
        patterns = item.get("dblp_toc_patterns") or item.get("dblp_toc_pattern") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        years = parse_years(item.get("years")) or default_years
        source = ConferenceSource(
            id=str(item.get("id") or slugify(str(item.get("name", "venue")))),
            name=str(item.get("name") or item.get("id") or "Venue"),
            group=str(item.get("group") or "conference"),
            dblp_toc_patterns=[str(pattern) for pattern in patterns if str(pattern).strip()],
            years=years,
            enabled=bool(item.get("enabled", True)),
        )
        if source.enabled and source.dblp_toc_patterns:
            sources.append(source)
    return sources


def github_request(url: str, token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "paper-daily-collector",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_json_block(markdown: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", markdown, flags=re.S | re.I)
    if fenced:
        return json.loads(fenced.group(1))
    stripped = markdown.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)
    return None


def load_issue_config(default_config: dict[str, Any]) -> dict[str, Any]:
    token = os.getenv("GITHUB_TOKEN", "")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    title = os.getenv("CONFIG_ISSUE_TITLE", "Research Interests")
    if not token or not repository:
        return default_config

    query = urllib.parse.urlencode({"state": "open", "per_page": "30"})
    url = f"https://api.github.com/repos/{repository}/issues?{query}"
    try:
        issues = github_request(url, token)
    except Exception as exc:
        print(f"Warning: cannot read GitHub issues, using config file: {exc}", file=sys.stderr)
        return default_config

    for issue in issues:
        if "pull_request" in issue:
            continue
        if issue.get("title", "").strip().lower() == title.lower():
            body = issue.get("body") or ""
            try:
                issue_config = extract_json_block(body)
            except json.JSONDecodeError as exc:
                print(f"Warning: config issue JSON is invalid, using config file: {exc}", file=sys.stderr)
                return default_config
            if issue_config and issue_config.get("topics"):
                return merge_config(default_config, issue_config)
    return default_config


def arxiv_query_for_topic(topic: Topic) -> str:
    keyword_terms = []
    for keyword in topic.keywords[:8]:
        escaped = keyword.replace('"', '\\"')
        keyword_terms.append(f'all:"{escaped}"')

    category_terms = [f"cat:{category}" for category in topic.arxiv_categories[:5]]
    query_mode = os.getenv("ARXIV_QUERY_MODE", "keyword").strip().lower()
    if query_mode == "keyword":
        if keyword_terms:
            return "(" + " OR ".join(keyword_terms) + ")"
        if category_terms:
            return "(" + " OR ".join(category_terms) + ")"
        return f'all:"{topic.name}"'

    if query_mode == "strict":
        parts = []
        if keyword_terms:
            parts.append("(" + " OR ".join(keyword_terms) + ")")
        if category_terms:
            parts.append("(" + " OR ".join(category_terms) + ")")
        return " AND ".join(parts) if parts else f'all:"{topic.name}"'

    terms = [*keyword_terms, *category_terms]
    if terms:
        return "(" + " OR ".join(terms) + ")"

    parts = []
    if keyword_terms:
        parts.append("(" + " OR ".join(keyword_terms) + ")")
    if category_terms:
        parts.append("(" + " OR ".join(category_terms) + ")")
    return " AND ".join(parts) if parts else f'all:"{topic.name}"'


def arxiv_category_query_for_topic(topic: Topic) -> str:
    category_terms = [f"cat:{category}" for category in topic.arxiv_categories[:5]]
    if category_terms:
        return "(" + " OR ".join(category_terms) + ")"
    return arxiv_query_for_topic(topic)


def topic_text_query(topic: Topic, limit: int = 6) -> str:
    terms = topic.keywords[:limit] or [topic.name]
    return " OR ".join(terms)


def topic_plain_query(topic: Topic, limit: int = 6) -> str:
    return " ".join(topic.keywords[:limit]) or topic.name


def html_to_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return normalize_space(html.unescape(without_tags))


def date_to_iso(value: str | int | None) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, int):
        return f"{value:04d}-01-01T00:00:00+00:00"
    parsed = parse_datetime(str(value))
    if parsed:
        return parsed.isoformat()
    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00+00:00"
    if re.fullmatch(r"\d{4}", text):
        return f"{text}-01-01T00:00:00+00:00"
    return text


def request_json(url: str, headers: dict[str, str] | None = None, timeout: float = 60) -> Any:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "paper-daily-collector/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request_bytes(url: str, headers: dict[str, str] | None = None, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "paper-daily-collector/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def source_request_headers(source: SourceConfig) -> dict[str, str]:
    headers = {"User-Agent": "paper-daily-collector/1.0"}
    if source.headers_env:
        raw_headers = os.getenv(source.headers_env, "")
        if raw_headers:
            try:
                configured_headers = json.loads(raw_headers)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source.headers_env} must contain a JSON object of HTTP headers") from exc
            if not isinstance(configured_headers, dict):
                raise ValueError(f"{source.headers_env} must contain a JSON object of HTTP headers")
            headers.update({str(key): str(value) for key, value in configured_headers.items()})
    if source.bearer_token_env:
        token = os.getenv(source.bearer_token_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def arxiv_retry_wait_seconds(exc: Exception, attempt: int) -> float:
    min_wait = float(os.getenv("ARXIV_RETRY_MIN_SECONDS", "45"))
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = exc.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return max(min_wait, float(retry_after))
    base = float(os.getenv("ARXIV_RETRY_BASE_SECONDS", "45"))
    cap = float(os.getenv("ARXIV_RETRY_MAX_SECONDS", "180"))
    return max(min_wait, min(cap, base * (2**attempt)))


def is_retryable_arxiv_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in TRANSIENT_HTTP_CODES
    return isinstance(exc, (TimeoutError, urllib.error.URLError, OSError))


def should_retry_arxiv_error(exc: Exception) -> bool:
    if not is_retryable_arxiv_error(exc):
        return False
    if isinstance(exc, urllib.error.HTTPError) and exc.code in {429, 503}:
        return env_flag("ARXIV_RETRY_THROTTLED", False)
    return True


def should_stop_arxiv_fetches(exc: Exception) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code in {429, 503}


def parse_arxiv_entries(xml_data: bytes, seed_topic: str = "") -> list[dict[str, Any]]:
    root = ET.fromstring(xml_data)
    papers = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        paper_id = entry.findtext("atom:id", default="", namespaces=ARXIV_NS).strip()
        title = normalize_space(entry.findtext("atom:title", default="", namespaces=ARXIV_NS))
        summary = normalize_space(entry.findtext("atom:summary", default="", namespaces=ARXIV_NS))
        published = entry.findtext("atom:published", default="", namespaces=ARXIV_NS)
        updated = entry.findtext("atom:updated", default="", namespaces=ARXIV_NS)
        authors = [
            normalize_space(author.findtext("atom:name", default="", namespaces=ARXIV_NS))
            for author in entry.findall("atom:author", ARXIV_NS)
        ]
        categories = [
            category.attrib.get("term", "")
            for category in entry.findall("atom:category", ARXIV_NS)
            if category.attrib.get("term")
        ]
        pdf_url = ""
        for link in entry.findall("atom:link", ARXIV_NS):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href", "")
                break
        papers.append(
            {
                "id": paper_id.rsplit("/", 1)[-1],
                "source": "arXiv",
                "title": title,
                "authors": [a for a in authors if a],
                "summary": summary,
                "published": published,
                "updated": updated,
                "paper_url": paper_id,
                "pdf_url": pdf_url or paper_id.replace("/abs/", "/pdf/"),
                "categories": categories,
                "seed_topic": seed_topic,
            }
        )
    return papers


def fetch_arxiv_query(search_query: str, max_results: int, sort_by: str, sort_order: str, label: str) -> list[dict[str, Any]]:
    params = {
        "search_query": search_query,
        "start": "0",
        "max_results": str(max_results),
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }
    url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"
    retry_count = max(1, int(os.getenv("ARXIV_RETRIES", "4")))
    timeout_seconds = float(os.getenv("ARXIV_TIMEOUT_SECONDS", "90"))
    last_error: Exception | None = None
    for attempt in range(retry_count):
        req = urllib.request.Request(url, headers={"User-Agent": "paper-daily-collector/1.0 (+https://github.com/Futuresxy/paper-daily)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                xml_data = resp.read()
            break
        except Exception as exc:
            last_error = exc
            if not should_retry_arxiv_error(exc) or attempt == retry_count - 1:
                raise
            wait_seconds = arxiv_retry_wait_seconds(exc, attempt)
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                print(f"arXiv rate limited {label}, retrying in {wait_seconds:.0f}s", flush=True)
            else:
                print(f"arXiv temporary error for {label}: {exc}; retrying in {wait_seconds:.0f}s", flush=True)
            time.sleep(wait_seconds)
    else:
        raise RuntimeError(f"arXiv request failed: {last_error}")
    return parse_arxiv_entries(xml_data)


def fetch_arxiv(topic: Topic, max_results: int) -> list[dict[str, Any]]:
    sort_by = os.getenv("ARXIV_SORT_BY", "lastUpdatedDate").strip() or "lastUpdatedDate"
    papers = fetch_arxiv_query(
        arxiv_query_for_topic(topic),
        max_results,
        sort_by=sort_by,
        sort_order="descending",
        label=topic.name,
    )
    if env_flag("ARXIV_EXPAND_CATEGORY_SEARCH", False) and topic.arxiv_categories:
        in_topic_delay = float(os.getenv("ARXIV_IN_TOPIC_DELAY_SECONDS", "3"))
        if in_topic_delay > 0:
            time.sleep(in_topic_delay)
        category_max_results = max(1, int(os.getenv("ARXIV_CATEGORY_MAX_RESULTS", str(max_results))))
        category_papers = fetch_arxiv_query(
            arxiv_category_query_for_topic(topic),
            category_max_results,
            sort_by=sort_by,
            sort_order="descending",
            label=f"{topic.name} categories",
        )
        papers = dedupe_papers([*papers, *category_papers])
    for paper in papers:
        paper["seed_topic"] = topic.id
    return papers


def title_match_key(title: str) -> str:
    text = html.unescape(title).lower()
    text = re.sub(r"\barxiv:\d{4}\.\d+(v\d+)?\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return normalize_space(text)


def titles_match(left: str, right: str) -> bool:
    left_key = title_match_key(left)
    right_key = title_match_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    return difflib.SequenceMatcher(None, left_key, right_key).ratio() >= env_float("ARXIV_TITLE_MATCH_RATIO", 0.90)


def arxiv_title_query(title: str) -> str:
    safe_title = normalize_space(title).replace('"', " ")
    if not safe_title:
        return 'all:""'
    # Exact title search first; arXiv still returns close matches when punctuation differs.
    return f'ti:"{safe_title}"'


def find_arxiv_by_title(title: str, max_results: int = 5) -> dict[str, Any] | None:
    papers = fetch_arxiv_query(
        arxiv_title_query(title),
        max_results=max(1, max_results),
        sort_by="relevance",
        sort_order="descending",
        label=f"title:{title[:80]}",
    )
    for candidate in papers:
        if titles_match(title, str(candidate.get("title") or "")) and has_meaningful_summary(candidate):
            return candidate
    return None


def semantic_scholar_paper_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    paper_id = str(item.get("paperId") or "")
    title = normalize_space(str(item.get("title") or ""))
    if not paper_id or not title:
        return None
    raw_authors = item.get("authors") or []
    raw_fields = item.get("fieldsOfStudy") or []
    authors = [str(author.get("name") or "") for author in raw_authors[:12] if isinstance(author, dict)]
    categories = [str(value) for value in raw_fields if value]
    venue = str(item.get("venue") or "")
    if venue:
        categories.append(venue)
    pdf_url = str((item.get("openAccessPdf") or {}).get("url") or "")
    return {
        "id": f"s2:{paper_id}",
        "source": "Semantic Scholar",
        "title": title,
        "authors": [author for author in authors if author],
        "summary": normalize_space(str(item.get("abstract") or "")),
        "published": date_to_iso(item.get("publicationDate") or item.get("year")),
        "updated": "",
        "paper_url": str(item.get("url") or f"https://www.semanticscholar.org/paper/{paper_id}"),
        "pdf_url": pdf_url,
        "categories": categories[:8],
    }


def find_semantic_scholar_by_title(title: str, max_results: int = 5) -> dict[str, Any] | None:
    params = {
        "query": title,
        "limit": str(min(max(1, max_results), 100)),
        "fields": "paperId,title,abstract,authors,year,publicationDate,url,openAccessPdf,venue,externalIds,fieldsOfStudy",
    }
    headers = {"User-Agent": "paper-daily-collector/1.0"}
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
    if api_key:
        headers["x-api-key"] = api_key
    url = f"{SEMANTIC_SCHOLAR_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    data = request_json(url, headers=headers, timeout=float(os.getenv("SEMANTIC_SCHOLAR_TIMEOUT_SECONDS", "60")))
    for item in data.get("data") or []:
        candidate = semantic_scholar_paper_from_item(item)
        if candidate and titles_match(title, candidate["title"]) and has_meaningful_summary(candidate):
            return candidate
    return None


def openalex_paper_from_work(work: dict[str, Any], source_name: str = "OpenAlex") -> dict[str, Any] | None:
    title = normalize_space(str(work.get("title") or ""))
    work_id = str(work.get("id") or work.get("doi") or title)
    if not title or not work_id:
        return None
    locations = work.get("locations") or []
    primary = work.get("primary_location") or {}
    best_oa = work.get("best_oa_location") or {}
    pdf_url = (
        primary.get("pdf_url")
        or best_oa.get("pdf_url")
        or next((location.get("pdf_url") for location in locations if location.get("pdf_url")), "")
    )
    authors = [
        str((authorship.get("author") or {}).get("display_name") or "")
        for authorship in work.get("authorships", [])
    ]
    concepts = [
        str(concept.get("display_name") or "")
        for concept in work.get("concepts", [])[:8]
        if concept.get("display_name")
    ]
    return {
        "id": f"openalex:{work_id.rsplit('/', 1)[-1]}",
        "source": source_name,
        "title": title,
        "authors": [author for author in authors if author],
        "summary": normalize_space(openalex_abstract_text(work)),
        "published": date_to_iso(work.get("publication_date") or work.get("publication_year")),
        "updated": "",
        "paper_url": str(work.get("doi") or work.get("id") or ""),
        "pdf_url": str(pdf_url or ""),
        "categories": concepts,
    }


def find_openalex_by_title(title: str, max_results: int = 5) -> dict[str, Any] | None:
    params = {
        "search": title,
        "per-page": str(min(max(1, max_results), 25)),
    }
    mailto = os.getenv("CONTACT_EMAIL") or os.getenv("OPENALEX_EMAIL")
    if mailto:
        params["mailto"] = mailto
    url = f"{OPENALEX_WORKS_URL}?{urllib.parse.urlencode(params)}"
    data = request_json(url, timeout=float(os.getenv("OPENALEX_TIMEOUT_SECONDS", "60")))
    for work in data.get("results", []):
        candidate = openalex_paper_from_work(work)
        if candidate and titles_match(title, candidate["title"]) and has_meaningful_summary(candidate):
            return candidate
    return None


def crossref_paper_from_item(item: dict[str, Any], source_name: str = "Crossref") -> dict[str, Any] | None:
    title = normalize_space(" ".join(str(part) for part in item.get("title", []) if part))
    doi = str(item.get("DOI") or "")
    paper_url = str(item.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
    if not title or not (doi or paper_url):
        return None
    authors = []
    for author in item.get("author", [])[:12]:
        name = normalize_space(f"{author.get('given', '')} {author.get('family', '')}")
        if name:
            authors.append(name)
    pdf_url = ""
    for link in item.get("link", []):
        if "pdf" in str(link.get("content-type", "")).lower() and link.get("URL"):
            pdf_url = str(link.get("URL"))
            break
    return {
        "id": f"crossref:{doi or slugify(title)}",
        "source": source_name,
        "title": title,
        "authors": authors,
        "summary": html_to_text(str(item.get("abstract") or "")),
        "published": crossref_date(item),
        "updated": "",
        "paper_url": paper_url,
        "pdf_url": pdf_url,
        "categories": [str(subject) for subject in item.get("subject", [])[:8]],
    }


def find_crossref_by_title(title: str, max_results: int = 5) -> dict[str, Any] | None:
    params = {
        "query.title": title,
        "rows": str(min(max(1, max_results), 20)),
        "sort": "score",
        "order": "desc",
    }
    mailto = os.getenv("CONTACT_EMAIL") or os.getenv("CROSSREF_EMAIL")
    if mailto:
        params["mailto"] = mailto
    headers = {"User-Agent": f"paper-daily-collector/1.0 (mailto:{mailto or 'unknown@example.com'})"}
    url = f"{CROSSREF_WORKS_URL}?{urllib.parse.urlencode(params)}"
    data = request_json(url, headers=headers, timeout=float(os.getenv("CROSSREF_TIMEOUT_SECONDS", "60")))
    for item in (data.get("message") or {}).get("items", []):
        candidate = crossref_paper_from_item(item)
        if candidate and titles_match(title, candidate["title"]) and has_meaningful_summary(candidate):
            return candidate
    return None


def find_conference_abstract_by_title(title: str, max_results: int = 5) -> dict[str, Any] | None:
    finders = {
        "arxiv": find_arxiv_by_title,
        "semantic_scholar": find_semantic_scholar_by_title,
        "semanticscholar": find_semantic_scholar_by_title,
        "openalex": find_openalex_by_title,
        "crossref": find_crossref_by_title,
    }
    for source_type in conference_abstract_sources():
        finder = finders.get(source_type.strip().lower())
        if not finder:
            continue
        try:
            candidate = finder(title, max_results=max_results)
        except Exception as exc:
            print(f"Warning: {source_type} title enrichment failed for {title[:80]}: {exc}", file=sys.stderr)
            continue
        if candidate and has_meaningful_summary(candidate):
            return candidate
    return None


def conference_abstract_sources() -> list[str]:
    sources = env_list("CONFERENCE_ABSTRACT_SOURCES", ["arxiv", "openalex", "crossref"])
    if env_flag("ENABLE_SEMANTIC_SCHOLAR", False):
        return sources
    return [
        source
        for source in sources
        if source.strip().lower() not in {"semantic_scholar", "semanticscholar", "semantic-scholar"}
    ]


def enrich_conference_paper_from_arxiv(paper: dict[str, Any], arxiv_paper: dict[str, Any]) -> bool:
    return enrich_conference_paper_from_candidate(paper, arxiv_paper, "arXiv")


def enrich_conference_paper_from_candidate(
    paper: dict[str, Any],
    candidate: dict[str, Any],
    abstract_source: str | None = None,
) -> bool:
    if not has_meaningful_summary(candidate):
        return False
    source = abstract_source or str(candidate.get("source") or "external")
    paper["summary"] = candidate["summary"]
    paper["abstract_source"] = source
    paper["enriched"] = True
    paper["abstract_source_id"] = candidate.get("id", "")
    paper["abstract_source_url"] = candidate.get("paper_url", "")
    if source.lower() == "arxiv":
        paper["arxiv_id"] = candidate.get("id", "")
        paper["arxiv_url"] = candidate.get("paper_url", "")
    paper["source"] = f"{paper.get('source', 'DBLP')} + {source}"
    if candidate.get("paper_url"):
        paper["paper_url"] = candidate["paper_url"]
    if candidate.get("pdf_url"):
        paper["pdf_url"] = candidate["pdf_url"]
    if candidate.get("authors"):
        paper["authors"] = candidate["authors"]
    categories = list(dict.fromkeys([*paper.get("categories", []), *candidate.get("categories", [])]))
    paper["categories"] = [category for category in categories if category]
    return True


def dblp_retry_wait_seconds(exc: Exception, attempt: int) -> float:
    min_wait = float(os.getenv("DBLP_RETRY_MIN_SECONDS", "5"))
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = exc.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return max(min_wait, float(retry_after))
    base = float(os.getenv("DBLP_RETRY_BASE_SECONDS", "5"))
    cap = float(os.getenv("DBLP_RETRY_MAX_SECONDS", "60"))
    return max(min_wait, min(cap, base * (2**attempt)))


def is_retryable_dblp_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in DBLP_TRANSIENT_HTTP_CODES
    return isinstance(exc, (TimeoutError, urllib.error.URLError, OSError, http.client.RemoteDisconnected))


def fetch_json_url(url: str, user_agent: str, timeout_seconds: float) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_dblp_json(query: str, max_results: int) -> dict[str, Any]:
    params = {
        "format": "json",
        "h": str(max_results),
        "q": query,
    }
    url = f"{DBLP_API_URL}?{urllib.parse.urlencode(params)}"
    retry_count = max(1, int(os.getenv("DBLP_RETRIES", "3")))
    timeout_seconds = float(os.getenv("DBLP_TIMEOUT_SECONDS", "45"))
    last_error: Exception | None = None
    for attempt in range(retry_count):
        try:
            return fetch_json_url(url, "paper-daily-collector/1.0 (+https://github.com/Futuresxy/paper-daily)", timeout_seconds)
        except Exception as exc:
            last_error = exc
            if not is_retryable_dblp_error(exc) or attempt == retry_count - 1:
                raise
            wait_seconds = dblp_retry_wait_seconds(exc, attempt)
            print(f"DBLP temporary error for query {query}: {exc}; retrying in {wait_seconds:.0f}s", flush=True)
            time.sleep(wait_seconds)
    raise RuntimeError(f"DBLP request failed: {last_error}")


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def parse_dblp_authors(info: dict[str, Any]) -> list[str]:
    authors = info.get("authors", {}).get("author", []) if isinstance(info.get("authors"), dict) else []
    names = []
    for author in ensure_list(authors):
        if isinstance(author, dict):
            name = author.get("text", "")
        else:
            name = str(author)
        name = normalize_space(str(name))
        if name:
            names.append(name)
    return names


def parse_dblp_hits(data: dict[str, Any], source: ConferenceSource, year: int, toc_key: str) -> list[dict[str, Any]]:
    hits_data = data.get("result", {}).get("hits", {}).get("hit", [])
    papers = []
    for hit in ensure_list(hits_data):
        if not isinstance(hit, dict):
            continue
        info = hit.get("info", {})
        if not isinstance(info, dict):
            continue
        key = str(info.get("key") or "")
        title = normalize_space(str(info.get("title") or ""))
        if not key or not title or key.endswith(f"/{year}"):
            continue
        venue = str(info.get("venue") or source.name)
        pages = str(info.get("pages") or "").strip()
        doi = str(info.get("doi") or "").strip()
        ee = str(info.get("ee") or "").strip()
        url = str(info.get("url") or "").strip()
        paper_url = url or f"https://dblp.org/rec/{key}"
        summary_parts = [f"DBLP 题录：{source.name} {year} 会议论文。"]
        if pages:
            summary_parts.append(f"页码：{pages}。")
        if doi:
            summary_parts.append(f"DOI：{doi}。")
        papers.append(
            {
                "id": f"dblp:{key}",
                "source": f"DBLP · {source.name}",
                "source_type": "conference",
                "title": html.unescape(title).rstrip("."),
                "authors": parse_dblp_authors(info),
                "summary": " ".join(summary_parts),
                "published": f"{year}-01-01T00:00:00+00:00",
                "updated": f"{year}-01-01T00:00:00+00:00",
                "paper_url": paper_url,
                "pdf_url": ee or paper_url,
                "categories": [source.name, source.group, str(year)],
                "venue": venue,
                "conference": {
                    "id": source.id,
                    "name": source.name,
                    "group": source.group,
                    "year": year,
                    "dblp_key": key,
                    "dblp_toc": toc_key,
                    "doi": doi,
                    "ee": ee,
                    "pages": pages,
                },
            }
        )
    return papers


def strip_html_tags(value: str) -> str:
    return normalize_space(re.sub(r"<[^>]+>", "", html.unescape(value)))


def dblp_html_url_for_toc(toc_key: str) -> str:
    path = toc_key
    if path.endswith(".bht"):
        path = path[:-4] + ".html"
    elif not path.endswith(".html"):
        path += ".html"
    return "http://dblp.org/" + path.lstrip("/")


def conference_paper_from_dblp_html_chunk(
    key: str,
    chunk: str,
    source: ConferenceSource,
    year: int,
    toc_key: str,
) -> dict[str, Any] | None:
    title_match = re.search(r'<span class="title"[^>]*>(.*?)</span>', chunk, flags=re.S)
    if not title_match:
        return None
    title = strip_html_tags(title_match.group(1)).rstrip(".")
    if not title or title.lower().startswith("proceedings of"):
        return None

    authors = [
        strip_html_tags(author)
        for author in re.findall(r'<span itemprop="name" title="([^"]+)">', chunk)
    ]
    pages_match = re.search(r'<span itemprop="pagination">(.*?)</span>', chunk, flags=re.S)
    pages = strip_html_tags(pages_match.group(1)) if pages_match else ""
    ee_match = re.search(r'<li class="ee">\s*<a href="([^"]+)"', chunk, flags=re.S)
    ee = html.unescape(ee_match.group(1)) if ee_match else ""
    paper_url = f"https://dblp.org/rec/{key}"
    summary_parts = [f"DBLP 题录：{source.name} {year} 会议论文。"]
    if pages:
        summary_parts.append(f"页码：{pages}。")
    return {
        "id": f"dblp:{key}",
        "source": f"DBLP · {source.name}",
        "source_type": "conference",
        "title": title,
        "authors": authors,
        "summary": " ".join(summary_parts),
        "published": f"{year}-01-01T00:00:00+00:00",
        "updated": f"{year}-01-01T00:00:00+00:00",
        "paper_url": paper_url,
        "pdf_url": ee or paper_url,
        "categories": [source.name, source.group, str(year)],
        "venue": source.name,
        "conference": {
            "id": source.id,
            "name": source.name,
            "group": source.group,
            "year": year,
            "dblp_key": key,
            "dblp_toc": toc_key,
            "doi": "",
            "ee": ee,
            "pages": pages,
        },
    }


def parse_dblp_html_toc(html_text: str, source: ConferenceSource, year: int, toc_key: str) -> list[dict[str, Any]]:
    starts = list(re.finditer(r'<li class="entry inproceedings" id="([^"]+)"', html_text))
    papers = []
    for index, match in enumerate(starts):
        key = html.unescape(match.group(1))
        if key.endswith(f"/{year}"):
            continue
        end = starts[index + 1].start() if index + 1 < len(starts) else len(html_text)
        paper = conference_paper_from_dblp_html_chunk(key, html_text[match.start():end], source, year, toc_key)
        if paper:
            papers.append(paper)
    return papers


def fetch_dblp_html_toc(toc_key: str, source: ConferenceSource, year: int) -> list[dict[str, Any]]:
    url = dblp_html_url_for_toc(toc_key)
    timeout_seconds = float(os.getenv("DBLP_TIMEOUT_SECONDS", "45"))
    retry_count = max(1, int(os.getenv("DBLP_RETRIES", "3")))
    last_error: Exception | None = None
    for attempt in range(retry_count):
        req = urllib.request.Request(url, headers={"User-Agent": "paper-daily-collector/1.0 (+https://github.com/Futuresxy/paper-daily)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                html_text = resp.read().decode("utf-8", "replace")
            return parse_dblp_html_toc(html_text, source, year, toc_key)
        except Exception as exc:
            last_error = exc
            if not is_retryable_dblp_error(exc) or attempt == retry_count - 1:
                raise
            wait_seconds = dblp_retry_wait_seconds(exc, attempt)
            print(f"DBLP temporary HTML error for {source.name} {year}: {exc}; retrying in {wait_seconds:.0f}s", flush=True)
            time.sleep(wait_seconds)
    raise RuntimeError(f"DBLP HTML request failed: {last_error}")


def fetch_dblp_conference(source: ConferenceSource, max_results: int) -> list[dict[str, Any]]:
    papers = []
    errors = []
    request_count = 0
    pattern_delay_seconds = float(os.getenv("DBLP_PATTERN_DELAY_SECONDS", "3"))
    for year in source.years:
        for pattern_index, pattern in enumerate(source.dblp_toc_patterns):
            if request_count:
                time.sleep(pattern_delay_seconds)
            toc_key = pattern.format(year=year)
            query = f"toc:{toc_key}:"
            request_count += 1
            try:
                data = fetch_dblp_json(query, max_results)
            except Exception as exc:
                try:
                    fallback_papers = fetch_dblp_html_toc(toc_key, source, year)
                except Exception as fallback_exc:
                    errors.append(fallback_exc)
                    print(
                        f"Warning: DBLP TOC request failed for {source.name} {year} pattern {pattern_index + 1}: {exc}; HTML fallback failed: {fallback_exc}",
                        file=sys.stderr,
                    )
                    continue
                papers.extend(fallback_papers[:max_results])
                continue
            papers.extend(parse_dblp_hits(data, source, year, toc_key))
    if not papers and errors:
        raise errors[-1]
    return dedupe_papers(papers)


def openalex_abstract_text(work: dict[str, Any]) -> str:
    inverted = work.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                words.append((position, str(word)))
    return " ".join(word for _, word in sorted(words))


def fetch_openalex(topic: Topic, max_results: int, source: SourceConfig) -> list[dict[str, Any]]:
    params = {
        "search": topic_plain_query(topic),
        "per-page": str(max_results),
        "sort": "publication_date:desc",
    }
    mailto = os.getenv("CONTACT_EMAIL") or os.getenv("OPENALEX_EMAIL")
    if mailto:
        params["mailto"] = mailto
    url = f"{OPENALEX_WORKS_URL}?{urllib.parse.urlencode(params)}"
    data = request_json(url, timeout=float(os.getenv("OPENALEX_TIMEOUT_SECONDS", "60")))
    papers = []
    for work in data.get("results", []):
        locations = work.get("locations") or []
        primary = work.get("primary_location") or {}
        best_oa = work.get("best_oa_location") or {}
        pdf_url = (
            primary.get("pdf_url")
            or best_oa.get("pdf_url")
            or next((location.get("pdf_url") for location in locations if location.get("pdf_url")), "")
        )
        authors = [
            str((authorship.get("author") or {}).get("display_name") or "")
            for authorship in work.get("authorships", [])
        ]
        concepts = [
            str(concept.get("display_name") or "")
            for concept in work.get("concepts", [])[:8]
            if concept.get("display_name")
        ]
        work_id = str(work.get("id") or work.get("doi") or work.get("title") or "")
        if not work_id:
            continue
        papers.append(
            {
                "id": f"openalex:{work_id.rsplit('/', 1)[-1]}",
                "source": source.name,
                "title": normalize_space(str(work.get("title") or "")),
                "authors": [author for author in authors if author],
                "summary": normalize_space(openalex_abstract_text(work)),
                "published": date_to_iso(work.get("publication_date") or work.get("publication_year")),
                "updated": "",
                "paper_url": str(work.get("doi") or work.get("id") or ""),
                "pdf_url": str(pdf_url or ""),
                "categories": concepts,
                "seed_topic": topic.id,
            }
        )
    return papers


def crossref_date(item: dict[str, Any]) -> str:
    for field in ("published-print", "published-online", "published", "created", "issued"):
        date_parts = (item.get(field) or {}).get("date-parts") or []
        if date_parts and date_parts[0]:
            parts = list(date_parts[0])
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            return dt.datetime(year, month, day, tzinfo=dt.timezone.utc).isoformat()
    return ""


def fetch_crossref(topic: Topic, max_results: int, source: SourceConfig) -> list[dict[str, Any]]:
    params = {
        "query.bibliographic": topic_plain_query(topic),
        "rows": str(max_results),
        "sort": "published",
        "order": "desc",
    }
    mailto = os.getenv("CONTACT_EMAIL") or os.getenv("CROSSREF_EMAIL")
    if mailto:
        params["mailto"] = mailto
    headers = {"User-Agent": f"paper-daily-collector/1.0 (mailto:{mailto or 'unknown@example.com'})"}
    url = f"{CROSSREF_WORKS_URL}?{urllib.parse.urlencode(params)}"
    data = request_json(url, headers=headers, timeout=float(os.getenv("CROSSREF_TIMEOUT_SECONDS", "60")))
    papers = []
    for item in (data.get("message") or {}).get("items", []):
        title = normalize_space(" ".join(str(part) for part in item.get("title", []) if part))
        doi = str(item.get("DOI") or "")
        paper_url = str(item.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
        if not title or not (doi or paper_url):
            continue
        authors = []
        for author in item.get("author", [])[:12]:
            name = normalize_space(f"{author.get('given', '')} {author.get('family', '')}")
            if name:
                authors.append(name)
        subjects = [str(subject) for subject in item.get("subject", [])[:8]]
        papers.append(
            {
                "id": f"crossref:{doi or slugify(title)}",
                "source": source.name,
                "title": title,
                "authors": authors,
                "summary": html_to_text(str(item.get("abstract") or "")),
                "published": crossref_date(item),
                "updated": "",
                "paper_url": paper_url,
                "pdf_url": "",
                "categories": subjects,
                "seed_topic": topic.id,
            }
        )
    return papers


def fetch_semantic_scholar(topic: Topic, max_results: int, source: SourceConfig) -> list[dict[str, Any]]:
    params = {
        "query": topic_plain_query(topic),
        "limit": str(min(max_results, 100)),
        "fields": "paperId,title,abstract,authors,year,publicationDate,url,openAccessPdf,venue,externalIds,fieldsOfStudy",
    }
    headers = {"User-Agent": "paper-daily-collector/1.0"}
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
    if api_key:
        headers["x-api-key"] = api_key
    url = f"{SEMANTIC_SCHOLAR_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    data = request_json(url, headers=headers, timeout=float(os.getenv("SEMANTIC_SCHOLAR_TIMEOUT_SECONDS", "60")))
    papers = []
    for item in data.get("data") or []:
        candidate = semantic_scholar_paper_from_item(item)
        if not candidate:
            continue
        candidate["source"] = source.name
        candidate["seed_topic"] = topic.id
        papers.append(candidate)
    return papers


def fetch_google_scholar_serpapi(topic: Topic, max_results: int, source: SourceConfig) -> list[dict[str, Any]]:
    api_key = os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")
    if not api_key:
        raise RuntimeError("SERPAPI_API_KEY is required for google_scholar_serpapi source")
    params = {
        "engine": "google_scholar",
        "q": topic_plain_query(topic),
        "num": str(min(max_results, 20)),
        "api_key": api_key,
    }
    data = request_json(
        f"{SERPAPI_SEARCH_URL}?{urllib.parse.urlencode(params)}",
        timeout=float(os.getenv("SERPAPI_TIMEOUT_SECONDS", "90")),
    )
    papers = []
    for item in data.get("organic_results", []):
        title = normalize_space(str(item.get("title") or ""))
        paper_url = str(item.get("link") or "")
        if not title or not paper_url:
            continue
        publication = item.get("publication_info") or {}
        publication_summary = str(publication.get("summary") or "")
        year_match = re.search(r"\b(19|20)\d{2}\b", publication_summary)
        resources = item.get("resources") or []
        pdf_url = next((str(resource.get("link")) for resource in resources if str(resource.get("file_format", "")).upper() == "PDF"), "")
        papers.append(
            {
                "id": f"google-scholar:{slugify(paper_url or title)}",
                "source": source.name,
                "title": title,
                "authors": [],
                "summary": normalize_space(" ".join([str(item.get("snippet") or ""), publication_summary])),
                "published": date_to_iso(year_match.group(0) if year_match else ""),
                "updated": "",
                "paper_url": paper_url,
                "pdf_url": pdf_url,
                "categories": ["Google Scholar"],
                "seed_topic": topic.id,
            }
        )
    return papers


def link_from_atom(entry: ET.Element) -> str:
    alternate = ""
    for link in entry.findall("atom:link", FEED_NAMESPACES):
        href = link.attrib.get("href", "")
        rel = link.attrib.get("rel", "alternate")
        if rel == "alternate" and href:
            return href
        if href and not alternate:
            alternate = href
    return alternate


def fetch_feed(source: SourceConfig, max_results: int) -> list[dict[str, Any]]:
    if not source.url:
        return []
    xml_data = request_bytes(
        source.url,
        headers=source_request_headers(source),
        timeout=float(os.getenv("FEED_TIMEOUT_SECONDS", "60")),
    )
    root = ET.fromstring(xml_data)
    papers = []
    atom_entries = root.findall("atom:entry", FEED_NAMESPACES)
    if root.tag.endswith("entry"):
        atom_entries = [root]
    for entry in atom_entries[:max_results]:
        title = normalize_space(entry.findtext("atom:title", default="", namespaces=FEED_NAMESPACES))
        summary = entry.findtext("atom:summary", default="", namespaces=FEED_NAMESPACES) or entry.findtext("atom:content", default="", namespaces=FEED_NAMESPACES)
        paper_url = link_from_atom(entry)
        paper_id = entry.findtext("atom:id", default=paper_url, namespaces=FEED_NAMESPACES)
        authors = [
            normalize_space(author.findtext("atom:name", default="", namespaces=FEED_NAMESPACES))
            for author in entry.findall("atom:author", FEED_NAMESPACES)
        ]
        categories = [category.attrib.get("term", "") for category in entry.findall("atom:category", FEED_NAMESPACES)]
        papers.append(
            {
                "id": f"feed:{slugify(source.name)}:{paper_id or paper_url or slugify(title)}",
                "source": source.name,
                "title": title,
                "authors": [author for author in authors if author],
                "summary": html_to_text(summary or ""),
                "published": date_to_iso(entry.findtext("atom:published", default="", namespaces=FEED_NAMESPACES)),
                "updated": date_to_iso(entry.findtext("atom:updated", default="", namespaces=FEED_NAMESPACES)),
                "paper_url": paper_url,
                "pdf_url": "",
                "categories": [category for category in categories if category],
                "seed_topic": "",
            }
        )

    for item in root.findall(".//channel/item")[:max_results]:
        title = normalize_space(item.findtext("title", default=""))
        paper_url = normalize_space(item.findtext("link", default=""))
        guid = normalize_space(item.findtext("guid", default=paper_url))
        papers.append(
            {
                "id": f"feed:{slugify(source.name)}:{guid or paper_url or slugify(title)}",
                "source": source.name,
                "title": title,
                "authors": [],
                "summary": html_to_text(item.findtext("description", default="")),
                "published": date_to_iso(item.findtext("pubDate", default="")),
                "updated": "",
                "paper_url": paper_url,
                "pdf_url": "",
                "categories": [],
                "seed_topic": "",
            }
        )
    return [paper for paper in papers if paper.get("title")]


def fetch_source_topic(source: SourceConfig, topic: Topic, max_results: int) -> list[dict[str, Any]]:
    if source.type == "arxiv":
        return fetch_arxiv(topic, max_results)
    if source.type == "openalex":
        return fetch_openalex(topic, max_results, source)
    if source.type == "crossref":
        return fetch_crossref(topic, max_results, source)
    if source.type == "semantic_scholar":
        return fetch_semantic_scholar(topic, max_results, source)
    if source.type == "google_scholar_serpapi":
        return fetch_google_scholar_serpapi(topic, max_results, source)
    raise ValueError(f"Unsupported topic source type: {source.type}")


def is_feed_source(source: SourceConfig) -> bool:
    return source.type in {"feed", "rss", "atom"}


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def paper_datetime(paper: dict[str, Any]) -> dt.datetime:
    for field in ("published", "updated", "last_seen_at", "first_seen_at"):
        parsed = parse_datetime(str(paper.get(field, "")))
        if parsed:
            return parsed
    return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def paper_activity_datetime(paper: dict[str, Any]) -> dt.datetime:
    for field in ("updated", "published", "last_seen_at", "first_seen_at"):
        parsed = parse_datetime(str(paper.get(field, "")))
        if parsed:
            return parsed
    return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def collection_cutoff(
    existing_payload: dict[str, Any],
    now: dt.datetime,
    days: int,
    incremental_since_last_run: bool,
) -> tuple[dt.datetime, str]:
    if incremental_since_last_run:
        previous_run = parse_datetime(
            str(existing_payload.get("generated_at_iso") or existing_payload.get("generated_at") or "")
        )
        if previous_run:
            return previous_run, "incremental"
    return now - dt.timedelta(days=max(0, days)), "lookback"


def keyword_score(topic: Topic, paper: dict[str, Any]) -> tuple[float, list[str]]:
    haystack = f"{paper.get('title', '')} {paper.get('summary', '')}".lower()
    hits = []
    weighted = 0.0
    for keyword in topic.keywords:
        normalized = keyword.lower()
        if normalized in haystack:
            hits.append(keyword)
            weighted += min(1.0, max(0.35, len(normalized.split()) / 5))
    score = min(1.0, weighted / max(2.0, min(5.0, len(topic.keywords) / 2)))
    return score, hits[:6]


def category_score(topic: Topic, paper: dict[str, Any]) -> float:
    paper_categories = set(paper.get("categories", []))
    topic_categories = set(topic.arxiv_categories)
    if not paper_categories or not topic_categories:
        return 0.0
    return len(paper_categories & topic_categories) / len(topic_categories)


def lexical_overlap_score(topic: Topic, paper: dict[str, Any]) -> float:
    topic_terms = set(re.findall(r"[a-zA-Z0-9]+", f"{topic.description} {' '.join(topic.keywords)}".lower()))
    paper_terms = set(re.findall(r"[a-zA-Z0-9]+", f"{paper.get('title', '')} {paper.get('summary', '')}".lower()))
    if not topic_terms or not paper_terms:
        return 0.0
    overlap = topic_terms & paper_terms
    return min(1.0, len(overlap) / max(8, len(topic_terms) * 0.18))


def match_level(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.42:
        return "medium"
    return "low"


def score_paper(topic: Topic, paper: dict[str, Any]) -> dict[str, Any]:
    k_score, hits = keyword_score(topic, paper)
    c_score = category_score(topic, paper)
    l_score = lexical_overlap_score(topic, paper)
    base_score = round(0.50 * k_score + 0.25 * c_score + 0.25 * l_score, 3)
    reason_parts = []
    if hits:
        reason_parts.append("关键词命中：" + "、".join(hits))
    if c_score > 0:
        reason_parts.append("arXiv 分类重合：" + "、".join(sorted(set(topic.arxiv_categories) & set(paper.get("categories", [])))))
    if not reason_parts:
        reason_parts.append("文本语义与方向描述存在弱相关，需要人工复核。")
    return {
        "topic_id": topic.id,
        "topic_name": topic.name,
        "score": base_score,
        "level": match_level(base_score),
        "reason": "；".join(reason_parts),
        "keyword_hits": hits,
    }


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def is_placeholder_conference_summary(paper: dict[str, Any]) -> bool:
    if paper.get("source_type") != "conference" or paper.get("abstract_source"):
        return False
    summary = normalize_space(str(paper.get("summary") or ""))
    return summary.startswith("DBLP 题录")


def has_meaningful_summary(paper: dict[str, Any], min_chars: int = 80) -> bool:
    if is_placeholder_conference_summary(paper):
        return False
    summary = normalize_space(str(paper.get("summary") or ""))
    return len(summary) >= min_chars


def is_relevant_enough(paper: dict[str, Any], best_match: dict[str, Any]) -> bool:
    if best_match.get("keyword_hits"):
        return True

    score = float(best_match.get("score") or 0.0)
    if paper.get("source_type") == "conference":
        return score >= env_float("MIN_CONFERENCE_SCORE", 0.18)
    if not has_meaningful_summary(paper):
        return score >= env_float("MIN_TITLE_ONLY_SCORE", 0.18)
    return score >= env_float("MIN_PAPER_SCORE", 0.08)


def enrich_conference_papers_from_arxiv(papers: list[dict[str, Any]]) -> dict[str, Any]:
    max_enrichments = max(
        0,
        int(os.getenv("MAX_CONFERENCE_ABSTRACT_ENRICHMENTS", os.getenv("MAX_CONFERENCE_ARXIV_ENRICHMENTS", "50"))),
    )
    delay_seconds = float(os.getenv("CONFERENCE_ABSTRACT_DELAY_SECONDS", os.getenv("CONFERENCE_ARXIV_DELAY_SECONDS", "3")))
    search_results = max(
        1,
        int(os.getenv("CONFERENCE_ABSTRACT_SEARCH_RESULTS", os.getenv("CONFERENCE_ARXIV_SEARCH_RESULTS", "5"))),
    )
    abstract_sources = conference_abstract_sources()
    arxiv_enrichment_enabled = "arxiv" in {source.strip().lower() for source in abstract_sources}
    stats: dict[str, Any] = {
        "conference_abstract_enrichment_attempted": 0,
        "conference_abstract_enrichment_succeeded": 0,
        "conference_abstract_enrichment_skipped": 0,
        "conference_abstract_enrichment_sources": abstract_sources,
        "conference_arxiv_enrichment_attempted": 0,
        "conference_arxiv_enrichment_succeeded": 0,
        "conference_arxiv_enrichment_skipped": 0,
        "conference_arxiv_enrichment_last_error": "",
    }
    if max_enrichments <= 0:
        return stats

    attempts = 0
    for paper in papers:
        if paper.get("source_type") != "conference" or has_meaningful_summary(paper):
            continue
        if attempts >= max_enrichments:
            stats["conference_abstract_enrichment_skipped"] += 1
            stats["conference_arxiv_enrichment_skipped"] += 1
            continue
        title = str(paper.get("title") or "")
        if not title:
            continue

        attempts += 1
        stats["conference_abstract_enrichment_attempted"] += 1
        if arxiv_enrichment_enabled:
            stats["conference_arxiv_enrichment_attempted"] += 1
        candidate = find_conference_abstract_by_title(title, max_results=search_results)
        if candidate and enrich_conference_paper_from_candidate(paper, candidate):
            stats["conference_abstract_enrichment_succeeded"] += 1
            if str(paper.get("abstract_source") or "").lower() == "arxiv":
                stats["conference_arxiv_enrichment_succeeded"] += 1
            print(f"Enriched conference paper from {paper.get('abstract_source')}: {paper.get('title')}", flush=True)

        if attempts < max_enrichments and delay_seconds > 0:
            time.sleep(delay_seconds)
    return stats


def should_summarize_paper_with_llm(paper: dict[str, Any]) -> bool:
    has_summary = has_meaningful_summary(paper)
    if paper.get("source_type") == "conference" and not has_summary:
        return env_flag("LLM_SUMMARIZE_CONFERENCE", False) and env_flag("LLM_SUMMARIZE_TITLE_ONLY", False)
    if paper.get("source_type") == "conference" and not env_flag("LLM_SUMMARIZE_CONFERENCE", True):
        return False
    if not has_summary and not env_flag("LLM_SUMMARIZE_TITLE_ONLY", False):
        return False
    return True


def fallback_summary(paper: dict[str, Any], best_match: dict[str, Any]) -> dict[str, str]:
    abstract = paper.get("summary", "")
    first_sentence = re.split(r"(?<=[.!?])\s+", abstract)[0] if abstract else ""
    if paper.get("source_type") == "conference" and not has_meaningful_summary(paper):
        return {
            "problem": "DBLP 题录没有摘要，且未在外部论文索引中找到足够可靠的同题论文摘要。",
            "method": "请打开论文链接查看方法和系统设计细节。",
            "innovation": "仅凭标题无法可靠判断创新点，已避免占用模型翻译额度。",
            "evidence": "题录信息来自会议索引，技术细节需要在原文中核验。",
            "limitations": "DBLP 通常不提供摘要；如果 arXiv、Semantic Scholar、OpenAlex 或 Crossref 暂未收录摘要，自动摘要会缺失。",
            "why_relevant": best_match.get("reason", "与配置方向存在文本匹配。"),
        }
    if not has_meaningful_summary(paper):
        return {
            "problem": "来源没有提供足够摘要，当前不调用模型做标题猜测。",
            "method": "请打开论文链接查看方法细节。",
            "innovation": "标题信息不足，无法可靠提取创新点。",
            "evidence": "证据不足，需要阅读全文核验。",
            "limitations": "缺少摘要会降低自动相关性和中文总结质量。",
            "why_relevant": best_match.get("reason", "与配置方向存在文本匹配。"),
        }
    return {
        "problem": "未配置模型 API，当前仅基于标题、摘要和关键词生成基础摘要。",
        "method": first_sentence[:300] if first_sentence else "请打开论文链接查看方法细节。",
        "innovation": "需要接入模型 API 后自动抽取更精确的中文创新点。",
        "evidence": "来源摘要可在论文原文中核验。",
        "limitations": "基础模式不会阅读全文，也不会进行深度技术对比。",
        "why_relevant": best_match.get("reason", "与配置方向存在文本匹配。"),
    }


def llm_enabled() -> bool:
    return bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY"))


def llm_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "paper-daily-collector/1.0",
    }


def call_openai_compatible(prompt: str) -> dict[str, Any]:
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
    base_url = os.getenv("LLM_BASE_URL", "")
    if not base_url:
        base_url = "https://api.deepseek.com/v1" if os.getenv("DEEPSEEK_API_KEY") else "https://api.openai.com/v1"
    model = os.getenv("LLM_MODEL", "deepseek-chat" if os.getenv("DEEPSEEK_API_KEY") else "gpt-4o-mini")
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "你是严谨的论文技术分析助手。只输出合法 JSON，不要输出 Markdown。",
            },
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=llm_headers(api_key),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def build_llm_prompt(topic: Topic, paper: dict[str, Any], base_match: dict[str, Any]) -> str:
    abstract_label = "摘要/题录信息" if paper.get("source_type") == "conference" else "摘要"
    return f"""
请根据论文标题、摘要、分类和我的研究方向，输出精确中文分析。目标不是逐句翻译，而是综合整篇摘要快速判断这篇论文是否值得阅读。
要求：
1. 先识别论文真正解决的问题、核心机制、实验或系统证据，再翻译成自然中文。
2. 不要夸大摘要中没有的信息；如果证据不足，请明确说明。
3. 相关性判断要严格，说明它具体匹配哪些关键词、场景或系统瓶颈。
4. 如果论文只是泛泛相关，请把 match_level 降为 medium 或 low，并在 why_relevant 里说明需要人工复核。

我的研究方向：
名称：{topic.name}
描述：{topic.description}
关键词：{", ".join(topic.keywords)}

论文信息：
标题：{paper.get("title", "")}
作者：{", ".join(paper.get("authors", [])[:8])}
arXiv 分类：{", ".join(paper.get("categories", []))}
{abstract_label}：{paper.get("summary", "")}

基础匹配信息：
分数：{base_match.get("score")}
等级：{base_match.get("level")}
原因：{base_match.get("reason")}

请输出 JSON，字段必须为：
{{
  "problem": "论文要解决的问题，中文，1-2句，避免空泛背景",
  "method": "核心方法，中文，2-3句，包含关键技术组件或系统流程",
  "innovation": "相对已有工作的具体创新点，中文，2-3点合并成一段",
  "evidence": "摘要中可核验的实验、理论或系统证据；没有则写证据不足",
  "limitations": "可能局限或需要阅读全文确认的点",
  "why_relevant": "为什么匹配我的研究方向",
  "match_score_adjustment": 0.0,
  "match_level": "high|medium|low"
}}
""".strip()


def summarize_with_llm(topic: Topic, paper: dict[str, Any], base_match: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    if not llm_enabled():
        return fallback_summary(paper, base_match), base_match

    prompt = build_llm_prompt(topic, paper, base_match)
    try:
        data = call_openai_compatible(prompt)
    except Exception as exc:
        print(f"Warning: LLM summary failed for {paper.get('id')}: {exc}", file=sys.stderr)
        return fallback_summary(paper, base_match), base_match

    summary = {
        "problem": str(data.get("problem", "")),
        "method": str(data.get("method", "")),
        "innovation": str(data.get("innovation", "")),
        "evidence": str(data.get("evidence", "")),
        "limitations": str(data.get("limitations", "")),
        "why_relevant": str(data.get("why_relevant", "")),
    }
    adjustment = float(data.get("match_score_adjustment", 0.0) or 0.0)
    adjusted_score = max(0.0, min(1.0, base_match["score"] + adjustment))
    adjusted_level = str(data.get("match_level") or match_level(adjusted_score)).lower()
    if adjusted_level not in {"high", "medium", "low"}:
        adjusted_level = match_level(adjusted_score)
    adjusted_match = dict(base_match)
    adjusted_match["score"] = round(adjusted_score, 3)
    adjusted_match["level"] = adjusted_level
    adjusted_match["llm_reason"] = summary["why_relevant"]
    return summary, adjusted_match


def summarize_one(args: tuple[Topic, dict[str, Any]]) -> tuple[str, dict[str, str], dict[str, Any]]:
    topic, paper = args
    paper_id = str(paper.get("id", ""))
    summary, adjusted_match = summarize_with_llm(topic, paper, paper["best_match"])
    return paper_id, summary, adjusted_match


def dedupe_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for paper in papers:
        key = paper.get("id") or paper.get("paper_url")
        if key in seen:
            continue
        seen.add(key)
        unique.append(paper)
    return unique


def paper_key(paper: dict[str, Any]) -> str:
    return str(paper.get("id") or paper.get("paper_url") or "")


def best_match_level(paper: dict[str, Any]) -> str:
    return str((paper.get("best_match") or {}).get("level") or "low").lower()


def conference_identity(paper: dict[str, Any]) -> tuple[str, int] | None:
    conference = paper.get("conference")
    if not isinstance(conference, dict):
        return None
    conference_id = str(conference.get("id") or "")
    try:
        year = int(conference.get("year"))
    except (TypeError, ValueError):
        return None
    if not conference_id or year <= 0:
        return None
    return conference_id, year


def cached_conference_years(existing_payload: dict[str, Any]) -> dict[str, set[int]]:
    cached: dict[str, set[int]] = {}
    papers = existing_payload.get("papers", []) if isinstance(existing_payload, dict) else []
    for paper in papers:
        if not isinstance(paper, dict) or paper.get("source_type") != "conference":
            continue
        identity = conference_identity(paper)
        if not identity:
            continue
        conference_id, year = identity
        cached.setdefault(conference_id, set()).add(year)
    return cached


def active_conference_years(sources: list[ConferenceSource]) -> dict[str, set[int]]:
    return {source.id: set(source.years) for source in sources}


def uncached_conference_years(source: ConferenceSource, cached_years_by_source: dict[str, set[int]]) -> list[int]:
    cached_years = cached_years_by_source.get(source.id, set())
    return [year for year in source.years if year not in cached_years]


def should_retain_conference_paper(
    paper: dict[str, Any],
    active_years_by_source: dict[str, set[int]] | None,
) -> bool:
    if paper.get("source_type") != "conference" or active_years_by_source is None:
        return False
    identity = conference_identity(paper)
    if not identity:
        return False
    conference_id, year = identity
    return year in active_years_by_source.get(conference_id, set())


def split_conference_payload(existing_payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    daily_payload = copy.deepcopy(existing_payload) if isinstance(existing_payload, dict) else {}
    conference_payload = copy.deepcopy(existing_payload) if isinstance(existing_payload, dict) else {}
    papers = existing_payload.get("papers", []) if isinstance(existing_payload, dict) else []
    daily_payload["papers"] = [
        paper for paper in papers if isinstance(paper, dict) and paper.get("source_type") != "conference"
    ]
    conference_payload["papers"] = [
        paper for paper in papers if isinstance(paper, dict) and paper.get("source_type") == "conference"
    ]
    return daily_payload, conference_payload


def load_existing_payload(output_path: Path) -> dict[str, Any]:
    if not output_path.exists():
        return {}
    try:
        return load_json(output_path)
    except Exception as exc:
        print(f"Warning: cannot read existing paper data, starting fresh: {exc}", file=sys.stderr)
        return {}


def merge_with_retained_papers(
    current_papers: list[dict[str, Any]],
    existing_payload: dict[str, Any],
    now: dt.datetime,
    recent_history_days: int,
    active_conference_years_by_source: dict[str, set[int]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    existing_papers = existing_payload.get("papers", []) if isinstance(existing_payload, dict) else []
    existing_generated_at = str(existing_payload.get("generated_at_iso") or existing_payload.get("generated_at") or now.isoformat())
    retained_by_key: dict[str, dict[str, Any]] = {}
    dropped_low = 0
    retained_recent = 0
    for paper in existing_papers:
        if not isinstance(paper, dict):
            continue
        key = paper_key(paper)
        if not key:
            continue
        seen_at = parse_datetime(str(paper.get("first_seen_at") or paper.get("last_seen_at") or existing_generated_at))
        is_recent = bool(
            recent_history_days > 0
            and seen_at
            and (now.date() - seen_at.date()).days <= recent_history_days
        )
        is_active_conference = (
            should_retain_conference_paper(paper, active_conference_years_by_source)
            and is_relevant_enough(paper, paper.get("best_match") or {})
        )
        if paper.get("source_type") == "conference" and active_conference_years_by_source is not None and not is_active_conference:
            dropped_low += 1
            continue
        if best_match_level(paper) in RETAINED_MATCH_LEVELS or is_recent or is_active_conference:
            retained_by_key[key] = paper
            if is_recent and best_match_level(paper) not in RETAINED_MATCH_LEVELS:
                retained_recent += 1
        else:
            dropped_low += 1

    merged = []
    seen = set()
    now_iso = now.isoformat()
    for paper in current_papers:
        key = paper_key(paper)
        previous = retained_by_key.get(key)
        if previous:
            paper.setdefault("first_seen_at", previous.get("first_seen_at") or existing_generated_at)
        else:
            paper.setdefault("first_seen_at", now_iso)
        paper["last_seen_at"] = now_iso
        paper["retained_from_previous_run"] = False
        merged.append(paper)
        if key:
            seen.add(key)

    retained_count = 0
    for key, paper in retained_by_key.items():
        if key in seen:
            continue
        retained = dict(paper)
        retained.setdefault("first_seen_at", existing_generated_at)
        retained.setdefault("last_seen_at", existing_generated_at)
        retained["retained_from_previous_run"] = True
        merged.append(retained)
        retained_count += 1

    return dedupe_papers(merged), {
        "retained_paper_count": retained_count,
        "retained_recent_low_count": retained_recent,
        "dropped_low_relevance_count": dropped_low,
    }


def deletion_sort_key(paper: dict[str, Any]) -> tuple[int, dt.datetime]:
    level = best_match_level(paper)
    if paper.get("source_type") == "conference":
        relevance_priority = 1
    else:
        relevance_priority = 0 if level == "low" else 2
    return relevance_priority, paper_datetime(paper)


def trim_papers_for_storage(
    payload: dict[str, Any],
    max_stored_papers: int,
    max_data_bytes: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    papers = list(payload.get("papers", []))
    removed_by_level = {"high": 0, "medium": 0, "low": 0, "unknown": 0}

    def projected_size() -> int:
        projected = dict(payload)
        projected["papers"] = papers
        return json_size_bytes(projected)

    data_bytes = projected_size()
    while papers and (
        (max_stored_papers > 0 and len(papers) > max_stored_papers)
        or (max_data_bytes > 0 and data_bytes > max_data_bytes)
    ):
        remove_index = min(range(len(papers)), key=lambda index: deletion_sort_key(papers[index]))
        removed = papers.pop(remove_index)
        level = best_match_level(removed)
        removed_by_level[level if level in removed_by_level else "unknown"] += 1
        data_bytes = projected_size()

    return papers, {
        "max_stored_papers": max_stored_papers,
        "max_data_bytes": max_data_bytes,
        "data_bytes": data_bytes,
        "storage_trimmed_count": sum(removed_by_level.values()),
        "storage_trimmed_by_level": removed_by_level,
    }


def collect(
    config_path: Path,
    output_path: Path,
    conference_output_path: Path,
    days: int,
    max_per_topic: int,
    max_summaries: int,
    max_new_papers: int,
    max_stored_papers: int,
    max_new_conference_papers: int,
    max_stored_conference_papers: int,
    max_data_bytes: int,
    incremental_since_last_run: bool,
    recent_history_days: int,
    clear_cache: bool,
) -> dict[str, Any]:
    default_config = load_json(config_path)
    config = load_issue_config(default_config)
    topics = parse_topics(config)
    sources = parse_sources(config)
    now = dt.datetime.now(dt.timezone.utc)
    conference_sources = parse_conference_sources(config, now)
    active_conference_years_by_source = active_conference_years(conference_sources)
    mixed_existing_payload = {} if clear_cache else load_existing_payload(output_path)
    existing_payload, migrated_conference_payload = split_conference_payload(mixed_existing_payload)
    stored_conference_payload = {} if clear_cache else load_existing_payload(conference_output_path)
    if stored_conference_payload.get("papers"):
        existing_conference_payload = stored_conference_payload
    else:
        existing_conference_payload = migrated_conference_payload
    cached_conference_years_by_source = cached_conference_years(existing_conference_payload)
    cutoff, collection_mode = collection_cutoff(existing_payload, now, days, incremental_since_last_run)
    all_candidates = []
    successful_fetches = 0
    failed_fetches = 0
    successful_conference_fetches = 0
    failed_conference_fetches = 0
    skipped_cached_conference_years = 0
    cached_conference_candidate_count = 0
    conference_enrichment_stats: dict[str, Any] = {
        "conference_arxiv_enrichment_attempted": 0,
        "conference_arxiv_enrichment_succeeded": 0,
        "conference_arxiv_enrichment_skipped": 0,
        "conference_arxiv_enrichment_last_error": "",
    }
    source_stats: dict[str, dict[str, Any]] = {}
    source_delay_seconds = float(os.getenv("SOURCE_DELAY_SECONDS", "3"))
    for source in sources:
        source_stats[source.name] = {"type": source.type, "successful_fetches": 0, "failed_fetches": 0}
        if not source.enabled:
            continue
        if is_feed_source(source):
            print(f"Fetching feed source: {source.name}", flush=True)
            try:
                feed_papers = fetch_feed(source, max_per_topic * max(1, len(topics)))
                all_candidates.extend(feed_papers)
                successful_fetches += 1
                source_stats[source.name]["successful_fetches"] += 1
            except Exception as exc:
                failed_fetches += 1
                source_stats[source.name]["failed_fetches"] += 1
                source_stats[source.name]["last_error"] = str(exc)
                print(f"Warning: feed source failed for {source.name}: {exc}", file=sys.stderr)
            time.sleep(source_delay_seconds)
            continue

        for index, topic in enumerate(topics):
            if index:
                if source.type == "arxiv":
                    time.sleep(float(os.getenv("ARXIV_DELAY_SECONDS", "15")))
                else:
                    time.sleep(source_delay_seconds)
            print(f"Fetching {source.name} papers for topic: {topic.name}", flush=True)
            try:
                topic_papers = fetch_source_topic(source, topic, max_per_topic)
                all_candidates.extend(topic_papers)
                successful_fetches += 1
                source_stats[source.name]["successful_fetches"] += 1
            except Exception as exc:
                failed_fetches += 1
                source_stats[source.name]["failed_fetches"] += 1
                source_stats[source.name]["last_error"] = str(exc)
                print(f"Warning: {source.name} request failed for {topic.name}: {exc}", file=sys.stderr)
                if source.type == "arxiv" and should_stop_arxiv_fetches(exc):
                    skipped = len(topics) - index - 1
                    failed_fetches += skipped
                    source_stats[source.name]["failed_fetches"] += skipped
                    if skipped:
                        print(
                            f"Stopping arXiv fetches after {exc}; skipped {skipped} remaining topic(s) to avoid further throttling.",
                            file=sys.stderr,
                        )
                    break

    max_per_conference = int(os.getenv("MAX_PER_CONFERENCE", "1000"))
    conference_delay_seconds = float(os.getenv("DBLP_DELAY_SECONDS", "5"))
    for index, source in enumerate(conference_sources):
        years_to_fetch = uncached_conference_years(source, cached_conference_years_by_source)
        skipped_cached_conference_years += len(source.years) - len(years_to_fetch)
        source_stats[source.name] = {
            "type": "conference",
            "successful_fetches": 0,
            "failed_fetches": 0,
            "skipped_cached_years": len(source.years) - len(years_to_fetch),
        }
        if not years_to_fetch:
            print(f"Skipping DBLP conference source from cache: {source.name} {', '.join(str(year) for year in source.years)}", flush=True)
            continue
        if index:
            time.sleep(conference_delay_seconds)
        source_to_fetch = ConferenceSource(
            id=source.id,
            name=source.name,
            group=source.group,
            dblp_toc_patterns=source.dblp_toc_patterns,
            years=years_to_fetch,
            enabled=source.enabled,
        )
        print(f"Fetching DBLP conference papers for source: {source.name} {', '.join(str(year) for year in years_to_fetch)}", flush=True)
        try:
            source_papers = fetch_dblp_conference(source_to_fetch, max_per_conference)
            all_candidates.extend(source_papers)
            successful_fetches += 1
            successful_conference_fetches += 1
            source_stats[source.name]["successful_fetches"] += 1
        except Exception as exc:
            failed_fetches += 1
            failed_conference_fetches += 1
            source_stats[source.name]["failed_fetches"] += 1
            source_stats[source.name]["last_error"] = str(exc)
            print(f"Warning: DBLP request failed for {source.name}: {exc}", file=sys.stderr)

    for cached_paper in existing_conference_payload.get("papers", []) if isinstance(existing_conference_payload, dict) else []:
        if not isinstance(cached_paper, dict) or cached_paper.get("source_type") != "conference":
            continue
        if not should_retain_conference_paper(cached_paper, active_conference_years_by_source):
            continue
        if has_meaningful_summary(cached_paper):
            continue
        all_candidates.append(copy.deepcopy(cached_paper))
        cached_conference_candidate_count += 1

    if successful_fetches == 0 and failed_fetches > 0 and (existing_payload.get("papers") or existing_conference_payload.get("papers")):
        print("All configured sources failed; preserving existing paper data.", file=sys.stderr)

    recent_papers = []
    daily_backfill_candidates = []
    filtered_low_relevance = 0
    raw_daily_candidate_count = 0
    daily_outside_cutoff_count = 0
    backfill_days = max(days, env_int("DAILY_BACKFILL_DAYS", 14))
    daily_backfill_cutoff = now - dt.timedelta(days=max(0, backfill_days))
    for paper in dedupe_papers(all_candidates):
        is_conference_paper = paper.get("source_type") == "conference"
        if not is_conference_paper:
            raw_daily_candidate_count += 1
        activity_at = paper_activity_datetime(paper)
        in_primary_window = is_conference_paper or activity_at >= cutoff
        in_backfill_window = (
            not is_conference_paper
            and not in_primary_window
            and activity_at >= daily_backfill_cutoff
        )
        if not in_primary_window and not in_backfill_window:
            if not is_conference_paper:
                daily_outside_cutoff_count += 1
            continue

        matches = [score_paper(topic, paper) for topic in topics]
        matches.sort(key=lambda item: item["score"], reverse=True)
        best_match = matches[0]
        if not is_relevant_enough(paper, best_match):
            filtered_low_relevance += 1
            continue
        paper["matches"] = matches
        paper["best_match"] = best_match
        if in_backfill_window:
            paper["backfilled_from_recent_arxiv"] = True
            daily_outside_cutoff_count += 1
            daily_backfill_candidates.append(paper)
        else:
            recent_papers.append(paper)

    recent_papers.sort(key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)), reverse=True)
    daily_recent_papers = [paper for paper in recent_papers if paper.get("source_type") != "conference"]
    conference_recent_papers = [paper for paper in recent_papers if paper.get("source_type") == "conference"]
    daily_backfill_added_count = 0
    min_daily_papers = max(0, env_int("MIN_DAILY_PAPERS", 8))
    if len(daily_recent_papers) < min_daily_papers and daily_backfill_candidates:
        daily_backfill_candidates.sort(
            key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)),
            reverse=True,
        )
        existing_daily_ids = {str(paper.get("id", "")) for paper in daily_recent_papers}
        for paper in daily_backfill_candidates:
            if len(daily_recent_papers) >= min_daily_papers:
                break
            paper_id = str(paper.get("id", ""))
            if paper_id in existing_daily_ids:
                continue
            existing_daily_ids.add(paper_id)
            daily_recent_papers.append(paper)
            daily_backfill_added_count += 1
    candidate_paper_count = len(daily_recent_papers) + len(conference_recent_papers)
    daily_candidate_paper_count = len(daily_recent_papers)
    conference_candidate_paper_count = len(conference_recent_papers)
    if max_new_papers > 0:
        daily_recent_papers = daily_recent_papers[:max_new_papers]
    if max_new_conference_papers > 0:
        conference_recent_papers = conference_recent_papers[:max_new_conference_papers]
    conference_enrichment_stats = enrich_conference_papers_from_arxiv(conference_recent_papers)
    if conference_enrichment_stats["conference_arxiv_enrichment_succeeded"]:
        for paper in conference_recent_papers:
            matches = [score_paper(topic, paper) for topic in topics]
            matches.sort(key=lambda item: item["score"], reverse=True)
            paper["matches"] = matches
            paper["best_match"] = matches[0]
        conference_recent_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
    recent_papers = sorted(
        [*daily_recent_papers, *conference_recent_papers],
        key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)),
        reverse=True,
    )
    summaries_by_id: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
    llm_jobs = []
    for paper in recent_papers[:max_summaries]:
        if not should_summarize_paper_with_llm(paper):
            continue
        best_topic = next(topic for topic in topics if topic.id == paper["best_match"]["topic_id"])
        llm_jobs.append((best_topic, paper))

    if llm_enabled() and llm_jobs:
        concurrency = max(1, int(os.getenv("LLM_CONCURRENCY", "2")))
        print(f"Summarizing {len(llm_jobs)} papers with LLM using concurrency={concurrency}", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(summarize_one, job) for job in llm_jobs]
            for future in concurrent.futures.as_completed(futures):
                paper_id, summary, adjusted_match = future.result()
                summaries_by_id[paper_id] = (summary, adjusted_match)
                print(f"Finished summary: {paper_id}", flush=True)
    else:
        for topic, paper in llm_jobs:
            summary, adjusted_match = summarize_with_llm(topic, paper, paper["best_match"])
            summaries_by_id[str(paper.get("id", ""))] = (summary, adjusted_match)

    for index, paper in enumerate(recent_papers):
        paper_id = str(paper.get("id", ""))
        if index < max_summaries and paper_id in summaries_by_id:
            summary, adjusted_match = summaries_by_id[paper_id]
            paper["chinese_summary"] = summary
            paper["best_match"] = adjusted_match
            paper["matches"] = [adjusted_match if m["topic_id"] == adjusted_match["topic_id"] else m for m in paper["matches"]]
        else:
            paper["chinese_summary"] = fallback_summary(paper, paper["best_match"])

    daily_recent_papers = [paper for paper in recent_papers if paper.get("source_type") != "conference"]
    conference_recent_papers = [paper for paper in recent_papers if paper.get("source_type") == "conference"]
    daily_merged_papers, daily_retention_stats = merge_with_retained_papers(
        daily_recent_papers, existing_payload, now, recent_history_days
    )
    conference_merged_papers, conference_retention_stats = merge_with_retained_papers(
        conference_recent_papers,
        existing_conference_payload,
        now,
        recent_history_days,
        active_conference_years_by_source,
    )
    daily_merged_papers.sort(key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)), reverse=True)
    conference_merged_papers.sort(key=lambda p: (p["best_match"]["score"], paper_activity_datetime(p)), reverse=True)

    base_stats = {
        "candidate_paper_count": candidate_paper_count,
        "daily_candidate_paper_count": daily_candidate_paper_count,
        "conference_candidate_paper_count": conference_candidate_paper_count,
        "raw_daily_candidate_count": raw_daily_candidate_count,
        "daily_outside_cutoff_count": daily_outside_cutoff_count,
        "daily_backfill_days": backfill_days,
        "daily_backfill_candidate_count": len(daily_backfill_candidates),
        "daily_backfill_added_count": daily_backfill_added_count,
        "min_daily_papers": min_daily_papers,
        "filtered_low_relevance_count": filtered_low_relevance,
        "days": days,
        "collection_mode": collection_mode,
        "collection_cutoff_iso": cutoff.isoformat(),
        "max_per_topic": max_per_topic,
        "max_new_papers": max_new_papers,
        "max_new_conference_papers": max_new_conference_papers,
        "sources": [source.__dict__ for source in sources],
        "conference_sources": [source.__dict__ for source in conference_sources],
        "source_stats": source_stats,
        "llm_enabled": llm_enabled(),
        "llm_concurrency": int(os.getenv("LLM_CONCURRENCY", "2")),
        "recent_history_days": recent_history_days,
        "successful_fetches": successful_fetches,
        "failed_fetches": failed_fetches,
        "successful_conference_fetches": successful_conference_fetches,
        "failed_conference_fetches": failed_conference_fetches,
        "skipped_cached_conference_years": skipped_cached_conference_years,
        "conference_source_count": len(conference_sources),
        "cached_conference_candidate_count": cached_conference_candidate_count,
        "clear_cache": clear_cache,
        **conference_enrichment_stats,
    }

    payload = {
        "generated_at": email.utils.format_datetime(now),
        "generated_at_iso": now.isoformat(),
        "config_source": "issue" if config is not default_config else "file",
        "data_kind": "daily",
        "topics": [topic.__dict__ for topic in topics],
        "papers": daily_merged_papers,
        "stats": {
            **base_stats,
            "paper_count": len(daily_merged_papers),
            "new_paper_count": len(daily_recent_papers),
            **daily_retention_stats,
        },
    }
    trimmed_papers, storage_stats = trim_papers_for_storage(payload, max_stored_papers, max_data_bytes)
    trimmed_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
    payload["papers"] = trimmed_papers
    payload["stats"].update(storage_stats)
    payload["stats"]["paper_count"] = len(trimmed_papers)
    payload["stats"]["data_bytes"] = json_size_bytes(payload)
    write_json(output_path, payload)

    conference_payload = {
        "generated_at": email.utils.format_datetime(now),
        "generated_at_iso": now.isoformat(),
        "config_source": "issue" if config is not default_config else "file",
        "data_kind": "conference",
        "topics": [topic.__dict__ for topic in topics],
        "papers": conference_merged_papers,
        "stats": {
            **base_stats,
            "paper_count": len(conference_merged_papers),
            "new_paper_count": len(conference_recent_papers),
            **conference_retention_stats,
        },
    }
    conference_trimmed_papers, conference_storage_stats = trim_papers_for_storage(
        conference_payload,
        max_stored_conference_papers,
        max_data_bytes,
    )
    conference_trimmed_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
    conference_payload["papers"] = conference_trimmed_papers
    conference_payload["stats"].update(conference_storage_stats)
    conference_payload["stats"]["paper_count"] = len(conference_trimmed_papers)
    conference_payload["stats"]["data_bytes"] = json_size_bytes(conference_payload)
    write_json(conference_output_path, conference_payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect papers and build static data for paper-daily.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--conference-output", type=Path, default=Path(os.getenv("CONFERENCE_OUTPUT", str(DEFAULT_CONFERENCE_OUTPUT))))
    parser.add_argument("--days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "7")))
    parser.add_argument("--max-per-topic", type=int, default=int(os.getenv("MAX_PER_TOPIC", "25")))
    parser.add_argument("--max-summaries", type=int, default=int(os.getenv("MAX_SUMMARIES", "40")))
    parser.add_argument("--max-new-papers", type=int, default=int(os.getenv("MAX_NEW_PAPERS", str(DEFAULT_MAX_NEW_PAPERS))))
    parser.add_argument("--max-stored-papers", type=int, default=int(os.getenv("MAX_STORED_PAPERS", str(DEFAULT_MAX_STORED_PAPERS))))
    parser.add_argument(
        "--max-new-conference-papers",
        type=int,
        default=int(os.getenv("MAX_NEW_CONFERENCE_PAPERS", str(DEFAULT_MAX_NEW_CONFERENCE_PAPERS))),
    )
    parser.add_argument(
        "--max-stored-conference-papers",
        type=int,
        default=int(os.getenv("MAX_STORED_CONFERENCE_PAPERS", str(DEFAULT_MAX_STORED_CONFERENCE_PAPERS))),
    )
    parser.add_argument("--max-data-bytes", type=int, default=int(os.getenv("MAX_DATA_BYTES", str(DEFAULT_MAX_DATA_BYTES))))
    parser.add_argument("--incremental-since-last-run", action="store_true", default=env_flag("INCREMENTAL_SINCE_LAST_RUN"))
    parser.add_argument("--recent-history-days", type=int, default=int(os.getenv("RECENT_HISTORY_DAYS", str(DEFAULT_RECENT_HISTORY_DAYS))))
    parser.add_argument("--clear-cache", action="store_true", default=env_flag("CLEAR_PAPER_CACHE"))
    args = parser.parse_args()
    payload = collect(
        args.config,
        args.output,
        args.conference_output,
        args.days,
        args.max_per_topic,
        args.max_summaries,
        args.max_new_papers,
        args.max_stored_papers,
        args.max_new_conference_papers,
        args.max_stored_conference_papers,
        args.max_data_bytes,
        args.incremental_since_last_run,
        args.recent_history_days,
        args.clear_cache,
    )
    print(f"Wrote {len(payload['papers'])} daily papers to {args.output}")
    stats = payload.get("stats", {})
    print(
        "Daily arXiv stats: "
        f"raw={stats.get('raw_daily_candidate_count', 0)}, "
        f"selected={stats.get('daily_candidate_paper_count', 0)}, "
        f"backfilled={stats.get('daily_backfill_added_count', 0)}, "
        f"filtered={stats.get('filtered_low_relevance_count', 0)}"
    )
    if args.conference_output.exists():
        conference_payload = load_json(args.conference_output)
        print(f"Wrote {len(conference_payload.get('papers', []))} conference papers to {args.conference_output}")


if __name__ == "__main__":
    main()
