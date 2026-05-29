#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import html
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
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
DEFAULT_CONFIG = Path("config/interests.json")
DEFAULT_OUTPUT = Path("web/data/papers.json")
RETAINED_MATCH_LEVELS = {"high", "medium"}
DEFAULT_MAX_STORED_PAPERS = 800
DEFAULT_MAX_DATA_BYTES = 8 * 1024 * 1024
DEFAULT_RECENT_HISTORY_DAYS = 45
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Topic:
    id: str
    name: str
    description: str
    keywords: list[str]
    arxiv_categories: list[str]


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
                return issue_config
    return default_config


def arxiv_query_for_topic(topic: Topic) -> str:
    keyword_terms = []
    for keyword in topic.keywords[:8]:
        escaped = keyword.replace('"', '\\"')
        keyword_terms.append(f'all:"{escaped}"')

    category_terms = [f"cat:{category}" for category in topic.arxiv_categories[:5]]
    parts = []
    if keyword_terms:
        parts.append("(" + " OR ".join(keyword_terms) + ")")
    if category_terms:
        parts.append("(" + " OR ".join(category_terms) + ")")
    return " AND ".join(parts) if parts else f'all:"{topic.name}"'


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


def should_stop_arxiv_fetches(exc: Exception) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code in {429, 503}


def fetch_arxiv(topic: Topic, max_results: int) -> list[dict[str, Any]]:
    params = {
        "search_query": arxiv_query_for_topic(topic),
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"
    retry_count = int(os.getenv("ARXIV_RETRIES", "4"))
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
            if not is_retryable_arxiv_error(exc) or attempt == retry_count - 1:
                raise
            wait_seconds = arxiv_retry_wait_seconds(exc, attempt)
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                print(f"arXiv rate limited {topic.name}, retrying in {wait_seconds:.0f}s", flush=True)
            else:
                print(f"arXiv temporary error for {topic.name}: {exc}; retrying in {wait_seconds:.0f}s", flush=True)
            time.sleep(wait_seconds)
    else:
        raise RuntimeError(f"arXiv request failed: {last_error}")

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
                "seed_topic": topic.id,
            }
        )
    return papers


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


def fallback_summary(paper: dict[str, Any], best_match: dict[str, Any]) -> dict[str, str]:
    abstract = paper.get("summary", "")
    first_sentence = re.split(r"(?<=[.!?])\s+", abstract)[0] if abstract else ""
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
    return f"""
请根据论文标题、摘要、分类和我的研究方向，输出精确中文分析。不要夸大摘要中没有的信息；如果证据不足，请明确说明。

我的研究方向：
名称：{topic.name}
描述：{topic.description}
关键词：{", ".join(topic.keywords)}

论文信息：
标题：{paper.get("title", "")}
作者：{", ".join(paper.get("authors", [])[:8])}
arXiv 分类：{", ".join(paper.get("categories", []))}
摘要：{paper.get("summary", "")}

基础匹配信息：
分数：{base_match.get("score")}
等级：{base_match.get("level")}
原因：{base_match.get("reason")}

请输出 JSON，字段必须为：
{{
  "problem": "论文要解决的问题，中文，1-2句",
  "method": "核心方法，中文，1-2句",
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
        if best_match_level(paper) in RETAINED_MATCH_LEVELS or is_recent:
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
    relevance_priority = 0 if level == "low" else 1
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
    days: int,
    max_per_topic: int,
    max_summaries: int,
    max_stored_papers: int,
    max_data_bytes: int,
    incremental_since_last_run: bool,
    recent_history_days: int,
) -> dict[str, Any]:
    default_config = load_json(config_path)
    config = load_issue_config(default_config)
    topics = parse_topics(config)
    now = dt.datetime.now(dt.timezone.utc)
    existing_payload = load_existing_payload(output_path)
    cutoff, collection_mode = collection_cutoff(existing_payload, now, days, incremental_since_last_run)
    all_candidates = []
    successful_fetches = 0
    failed_fetches = 0
    for index, topic in enumerate(topics):
        if index:
            time.sleep(float(os.getenv("ARXIV_DELAY_SECONDS", "15")))
        print(f"Fetching arXiv papers for topic: {topic.name}", flush=True)
        try:
            topic_papers = fetch_arxiv(topic, max_per_topic)
            all_candidates.extend(topic_papers)
            successful_fetches += 1
        except Exception as exc:
            failed_fetches += 1
            print(f"Warning: arXiv request failed for {topic.name}: {exc}", file=sys.stderr)
            if should_stop_arxiv_fetches(exc):
                skipped = len(topics) - index - 1
                failed_fetches += skipped
                if skipped:
                    print(
                        f"Stopping arXiv fetches after {exc}; skipped {skipped} remaining topic(s) to avoid further throttling.",
                        file=sys.stderr,
                    )
                break

    if failed_fetches == len(topics) and existing_payload:
        existing = existing_payload
        if existing.get("papers"):
            print("All sources failed; preserving existing paper data.", file=sys.stderr)
            retained_papers, retention_stats = merge_with_retained_papers(
                [], existing_payload, now, recent_history_days
            )
            retained_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
            existing["papers"] = retained_papers
            existing["generated_at"] = email.utils.format_datetime(now)
            existing["generated_at_iso"] = now.isoformat()
            existing_stats = existing.setdefault("stats", {})
            existing_stats.update(
                {
                    "last_error": "All arXiv requests failed.",
                    "successful_fetches": successful_fetches,
                    "failed_fetches": failed_fetches,
                    **retention_stats,
                }
            )
            trimmed_papers, storage_stats = trim_papers_for_storage(existing, max_stored_papers, max_data_bytes)
            trimmed_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
            existing["papers"] = trimmed_papers
            existing_stats.update(storage_stats)
            existing_stats["paper_count"] = len(trimmed_papers)
            existing_stats["data_bytes"] = json_size_bytes(existing)
            write_json(output_path, existing)
            return existing

    recent_papers = []
    for paper in dedupe_papers(all_candidates):
        published = paper.get("published") or paper.get("updated")
        if not published:
            continue
        published_at = parse_datetime(str(published))
        if published_at and published_at >= cutoff:
            matches = [score_paper(topic, paper) for topic in topics]
            matches.sort(key=lambda item: item["score"], reverse=True)
            best_match = matches[0]
            if best_match["score"] <= 0:
                continue
            paper["matches"] = matches
            paper["best_match"] = best_match
            recent_papers.append(paper)

    recent_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
    summaries_by_id: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
    llm_jobs = []
    for paper in recent_papers[:max_summaries]:
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

    merged_papers, retention_stats = merge_with_retained_papers(
        recent_papers, existing_payload, now, recent_history_days
    )
    merged_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)

    payload = {
        "generated_at": email.utils.format_datetime(now),
        "generated_at_iso": now.isoformat(),
        "config_source": "issue" if config is not default_config else "file",
        "topics": [topic.__dict__ for topic in topics],
        "papers": merged_papers,
        "stats": {
            "paper_count": len(merged_papers),
            "new_paper_count": len(recent_papers),
            "days": days,
            "collection_mode": collection_mode,
            "collection_cutoff_iso": cutoff.isoformat(),
            "max_per_topic": max_per_topic,
            "llm_enabled": llm_enabled(),
            "llm_concurrency": int(os.getenv("LLM_CONCURRENCY", "2")),
            "recent_history_days": recent_history_days,
            "successful_fetches": successful_fetches,
            "failed_fetches": failed_fetches,
            **retention_stats,
        },
    }
    trimmed_papers, storage_stats = trim_papers_for_storage(payload, max_stored_papers, max_data_bytes)
    trimmed_papers.sort(key=lambda p: (p["best_match"]["score"], p.get("published", "")), reverse=True)
    payload["papers"] = trimmed_papers
    payload["stats"].update(storage_stats)
    payload["stats"]["paper_count"] = len(trimmed_papers)
    payload["stats"]["data_bytes"] = json_size_bytes(payload)
    write_json(output_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect papers and build static data for paper-daily.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "7")))
    parser.add_argument("--max-per-topic", type=int, default=int(os.getenv("MAX_PER_TOPIC", "25")))
    parser.add_argument("--max-summaries", type=int, default=int(os.getenv("MAX_SUMMARIES", "40")))
    parser.add_argument("--max-stored-papers", type=int, default=int(os.getenv("MAX_STORED_PAPERS", str(DEFAULT_MAX_STORED_PAPERS))))
    parser.add_argument("--max-data-bytes", type=int, default=int(os.getenv("MAX_DATA_BYTES", str(DEFAULT_MAX_DATA_BYTES))))
    parser.add_argument("--incremental-since-last-run", action="store_true", default=env_flag("INCREMENTAL_SINCE_LAST_RUN"))
    parser.add_argument("--recent-history-days", type=int, default=int(os.getenv("RECENT_HISTORY_DAYS", str(DEFAULT_RECENT_HISTORY_DAYS))))
    args = parser.parse_args()
    payload = collect(
        args.config,
        args.output,
        args.days,
        args.max_per_topic,
        args.max_summaries,
        args.max_stored_papers,
        args.max_data_bytes,
        args.incremental_since_last_run,
        args.recent_history_days,
    )
    print(f"Wrote {len(payload['papers'])} papers to {args.output}")


if __name__ == "__main__":
    main()
