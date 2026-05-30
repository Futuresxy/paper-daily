import datetime as dt
import os
import urllib.error
import unittest
from unittest import mock

from scripts.collect_papers import (
    arxiv_retry_wait_seconds,
    collection_cutoff,
    fetch_arxiv,
    is_retryable_arxiv_error,
    merge_with_retained_papers,
    openalex_abstract_text,
    parse_sources,
    should_retry_arxiv_error,
    source_request_headers,
    SourceConfig,
    Topic,
    trim_papers_for_storage,
)


def paper(paper_id: str, level: str, published: str) -> dict:
    return {
        "id": paper_id,
        "title": paper_id,
        "published": published,
        "best_match": {
            "topic_id": "topic",
            "topic_name": "Topic",
            "score": {"high": 0.9, "medium": 0.5, "low": 0.2}[level],
            "level": level,
            "reason": "test",
        },
        "matches": [],
        "chinese_summary": {},
    }


class RetentionTest(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("ARXIV_RETRY_MIN_SECONDS", None)
        os.environ.pop("ARXIV_RETRY_BASE_SECONDS", None)
        os.environ.pop("ARXIV_RETRY_MAX_SECONDS", None)
        os.environ.pop("ARXIV_RETRY_THROTTLED", None)
        os.environ.pop("CUSTOM_FEED_HEADERS", None)
        os.environ.pop("CUSTOM_FEED_BEARER_TOKEN", None)

    def test_arxiv_retry_wait_uses_retry_after_header(self) -> None:
        os.environ["ARXIV_RETRY_MIN_SECONDS"] = "30"
        error = urllib.error.HTTPError(
            "https://export.arxiv.org/api/query",
            429,
            "Too Many Requests",
            {"Retry-After": "75"},
            None,
        )

        self.assertEqual(arxiv_retry_wait_seconds(error, 0), 75.0)

    def test_arxiv_retry_wait_clamps_short_retry_after_header(self) -> None:
        os.environ["ARXIV_RETRY_MIN_SECONDS"] = "30"
        error = urllib.error.HTTPError(
            "https://export.arxiv.org/api/query",
            503,
            "Service Unavailable",
            {"Retry-After": "0"},
            None,
        )

        self.assertEqual(arxiv_retry_wait_seconds(error, 0), 30.0)

    def test_arxiv_retry_wait_uses_capped_backoff(self) -> None:
        os.environ["ARXIV_RETRY_MIN_SECONDS"] = "5"
        os.environ["ARXIV_RETRY_BASE_SECONDS"] = "10"
        os.environ["ARXIV_RETRY_MAX_SECONDS"] = "25"

        self.assertEqual(arxiv_retry_wait_seconds(TimeoutError("timed out"), 0), 10.0)
        self.assertEqual(arxiv_retry_wait_seconds(TimeoutError("timed out"), 2), 25.0)

    def test_arxiv_retryable_errors(self) -> None:
        rate_limited = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
        not_found = urllib.error.HTTPError("url", 404, "Not Found", {}, None)

        self.assertTrue(is_retryable_arxiv_error(rate_limited))
        self.assertTrue(is_retryable_arxiv_error(TimeoutError("timed out")))
        self.assertFalse(is_retryable_arxiv_error(not_found))

    def test_arxiv_retry_policy_fast_fails_throttling_by_default(self) -> None:
        rate_limited = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
        service_unavailable = urllib.error.HTTPError("url", 503, "Service Unavailable", {}, None)
        gateway_error = urllib.error.HTTPError("url", 502, "Bad Gateway", {}, None)

        self.assertFalse(should_retry_arxiv_error(rate_limited))
        self.assertFalse(should_retry_arxiv_error(service_unavailable))
        self.assertTrue(should_retry_arxiv_error(gateway_error))

    def test_arxiv_retry_policy_can_retry_throttling_when_enabled(self) -> None:
        os.environ["ARXIV_RETRY_THROTTLED"] = "true"
        rate_limited = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
        service_unavailable = urllib.error.HTTPError("url", 503, "Service Unavailable", {}, None)

        self.assertTrue(should_retry_arxiv_error(rate_limited))
        self.assertTrue(should_retry_arxiv_error(service_unavailable))

    def test_fetch_arxiv_does_not_sleep_on_service_unavailable_by_default(self) -> None:
        topic = Topic(
            id="llm_quant",
            name="LLM quantization",
            description="",
            keywords=["LLM quantization"],
            arxiv_categories=["cs.CL"],
        )
        service_unavailable = urllib.error.HTTPError(
            "https://export.arxiv.org/api/query",
            503,
            "Service Unavailable",
            {},
            None,
        )

        with (
            mock.patch("scripts.collect_papers.urllib.request.urlopen", side_effect=service_unavailable),
            mock.patch("scripts.collect_papers.time.sleep") as sleep_mock,
        ):
            with self.assertRaises(urllib.error.HTTPError):
                fetch_arxiv(topic, 1)

        sleep_mock.assert_not_called()

    def test_parse_sources_supports_custom_feed(self) -> None:
        sources = parse_sources(
            {
                "sources": [
                    "arxiv",
                    {
                        "type": "feed",
                        "name": "Journal Feed",
                        "url": "https://example.com/rss.xml",
                        "headers_env": "CUSTOM_FEED_HEADERS",
                    },
                    {"type": "crossref", "enabled": False},
                ]
            }
        )

        self.assertEqual([source.type for source in sources], ["arxiv", "feed"])
        self.assertEqual(sources[1].name, "Journal Feed")
        self.assertEqual(sources[1].url, "https://example.com/rss.xml")
        self.assertEqual(sources[1].headers_env, "CUSTOM_FEED_HEADERS")

    def test_source_request_headers_reads_secret_envs(self) -> None:
        os.environ["CUSTOM_FEED_HEADERS"] = '{"X-API-Key": "secret"}'
        os.environ["CUSTOM_FEED_BEARER_TOKEN"] = "token"

        headers = source_request_headers(
            SourceConfig(
                type="feed",
                name="Private Feed",
                url="https://example.com/feed.xml",
                headers_env="CUSTOM_FEED_HEADERS",
                bearer_token_env="CUSTOM_FEED_BEARER_TOKEN",
            )
        )

        self.assertEqual(headers["X-API-Key"], "secret")
        self.assertEqual(headers["Authorization"], "Bearer token")

    def test_openalex_abstract_text_reconstructs_inverted_index(self) -> None:
        abstract = openalex_abstract_text({"abstract_inverted_index": {"hello": [0], "world": [1]}})

        self.assertEqual(abstract, "hello world")

    def test_merge_retains_previous_high_medium_and_recent_low(self) -> None:
        now = dt.datetime(2026, 5, 28, tzinfo=dt.timezone.utc)
        stale_low = paper("old-low", "low", "2026-03-01T00:00:00+00:00")
        stale_low["first_seen_at"] = "2026-03-02T00:00:00+00:00"
        existing = {
            "generated_at_iso": "2026-05-27T00:00:00+00:00",
            "papers": [
                paper("old-high", "high", "2026-05-26T00:00:00+00:00"),
                paper("old-medium", "medium", "2026-05-25T00:00:00+00:00"),
                paper("recent-low", "low", "2026-05-24T00:00:00+00:00"),
                stale_low,
            ],
        }

        merged, stats = merge_with_retained_papers(
            [paper("new-low", "low", "2026-05-28T00:00:00+00:00")],
            existing,
            now,
            recent_history_days=45,
        )

        self.assertEqual({item["id"] for item in merged}, {"new-low", "old-high", "old-medium", "recent-low"})
        self.assertEqual(stats["retained_paper_count"], 3)
        self.assertEqual(stats["retained_recent_low_count"], 1)
        self.assertEqual(stats["dropped_low_relevance_count"], 1)
        self.assertTrue(next(item for item in merged if item["id"] == "old-high")["retained_from_previous_run"])

    def test_collection_cutoff_uses_previous_run_for_incremental_mode(self) -> None:
        now = dt.datetime(2026, 5, 28, 22, tzinfo=dt.timezone.utc)
        cutoff, mode = collection_cutoff(
            {"generated_at_iso": "2026-05-27T22:00:00+00:00"},
            now,
            days=7,
            incremental_since_last_run=True,
        )

        self.assertEqual(mode, "incremental")
        self.assertEqual(cutoff, dt.datetime(2026, 5, 27, 22, tzinfo=dt.timezone.utc))

    def test_collection_cutoff_falls_back_to_lookback(self) -> None:
        now = dt.datetime(2026, 5, 28, 22, tzinfo=dt.timezone.utc)
        cutoff, mode = collection_cutoff({}, now, days=7, incremental_since_last_run=True)

        self.assertEqual(mode, "lookback")
        self.assertEqual(cutoff, dt.datetime(2026, 5, 21, 22, tzinfo=dt.timezone.utc))

    def test_storage_trim_removes_low_then_oldest(self) -> None:
        payload = {
            "generated_at_iso": "2026-05-28T00:00:00+00:00",
            "papers": [
                paper("newer-high", "high", "2026-05-28T00:00:00+00:00"),
                paper("older-high", "high", "2026-05-20T00:00:00+00:00"),
                paper("newer-low", "low", "2026-05-28T00:00:00+00:00"),
            ],
            "stats": {},
        }

        trimmed, stats = trim_papers_for_storage(payload, max_stored_papers=2, max_data_bytes=0)
        self.assertEqual({item["id"] for item in trimmed}, {"newer-high", "older-high"})
        self.assertEqual(stats["storage_trimmed_by_level"]["low"], 1)

        payload["papers"] = trimmed
        trimmed, stats = trim_papers_for_storage(payload, max_stored_papers=1, max_data_bytes=0)
        self.assertEqual([item["id"] for item in trimmed], ["newer-high"])
        self.assertEqual(stats["storage_trimmed_by_level"]["high"], 1)


if __name__ == "__main__":
    unittest.main()
