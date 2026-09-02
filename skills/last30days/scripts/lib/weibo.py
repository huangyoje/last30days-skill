"""Weibo (微博) — Chinese microblog source for last30days.

Searches Weibo's public mobile search endpoint for statuses matching the
research topic. Uses a Cookie (``WEIBO_COOKIE`` in the config) because the
search endpoint returns empty/redirect for anonymous callers; the cookie is
read-only and never sent anywhere except weibo.cn.

Activation gate: this source is only available when a Weibo cookie is
configured. Unlike Xueqiu (financial gate), Weibo is a general-purpose
platform and applies to any topic. See ``pipeline.available_sources``.

Search model: unlike Xueqiu (no keyword endpoint), Weibo's mobile search
does accept a keyword directly, so this adapter follows the search-listing
pattern: hit the search-all container, decode ``mblog`` payloads out of the
card groups, and keep statuses whose text shares informative tokens with
the topic.

Endpoints used:
- GET /api/container/getIndex?containerid=100103type%3D1%26q=<kw>  -> 综合搜索
- GET /api/container/getIndex?containerid=100103type%3D61%26q=<kw> -> 实时搜索
"""

from __future__ import annotations

import http.cookiejar as _py_http_cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import http, log
from .relevance import token_overlap_relevance

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.6 Mobile/15E148 Safari/604.1"
)
_REFERER = "https://m.weibo.cn/"
_TIMEOUT = 10
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB safety cap (search pages are chunky)

# Config key that carries the Cookie string ("name=value; name2=value2").
COOKIE_CONFIG_KEY = "WEIBO_COOKIE"

# Per-depth result counts.
DEPTH_CONFIG = {
    "quick": 8,
    "default": 15,
    "deep": 25,
}

# CN timezone offset. Weibo timestamps are all Asia/Shanghai even when the
# tz suffix in the raw string says +0800; keep parsing deterministic.
_CST = timezone(timedelta(hours=8))

_cookie_jar = _py_http_cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookie_jar),
)
_cookies_loaded = False


def _log(msg: str) -> None:
    log.source_log("Weibo", msg, tty_only=False)


def _today() -> datetime:
    return datetime.now(_CST)


def _load_cookie_from_config(config: Optional[Dict[str, Any]]) -> bool:
    """Load the cookie string from config into the shared jar. Idempotent."""
    global _cookies_loaded
    if _cookies_loaded:
        return True
    cookie_str = (config or {}).get(COOKIE_CONFIG_KEY) or ""
    if not cookie_str:
        return False
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        try:
            _cookie_jar.set_cookie(
                _py_http_cookiejar.Cookie(
                    version=0,
                    name=name.strip(),
                    value=value.strip(),
                    port=None,
                    port_specified=False,
                    domain=".weibo.cn",
                    domain_specified=True,
                    domain_initial_dot=True,
                    path="/",
                    path_specified=True,
                    secure=True,
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                )
            )
        except Exception as exc:  # malformed single cookie: skip, keep others
            _log(f"skip malformed cookie pair {name!r}: {exc}")
    _cookies_loaded = True
    return True


def _get_json(url: str, config: Optional[Dict[str, Any]] = None) -> Any:
    """Fetch JSON with cookie-aware opener. Raises HTTPError on failure."""
    _load_cookie_from_config(config)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Referer": _REFERER,
            "X-Requested-With": "XMLHttpRequest",
            "MWeibo-Pwa": "1",
        },
    )
    try:
        with _opener.open(req, timeout=_TIMEOUT) as resp:
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise http.HTTPError(f"Weibo HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise http.HTTPError(f"Weibo request failed: {exc.reason}") from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise http.HTTPError("Weibo response exceeds the 4 MiB safety limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise http.HTTPError(f"Weibo JSON decode failed: {exc}") from exc


def _strip_html(text: str) -> str:
    """Remove HTML tags/entities from Weibo mblog.text."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    return re.sub(r"\s+", " ", text).strip()


def _parse_created_at(value: Any) -> Optional[str]:
    """Convert a Weibo ``created_at`` value to YYYY-MM-DD (CST) or None.

    Weibo returns one of several shapes:
    - RFC 2822-like: "Mon Sep 01 12:34:56 +0800 2025"
    - Relative CN:   "刚刚", "N分钟前", "N小时前", "昨天 HH:MM"
    - Absolute CN:   "MM-DD" (same year), "YYYY-MM-DD"
    - ISO 8601:      "2025-09-01T12:34:56+08:00"
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # RFC 2822-like — the most common shape on the mobile search API
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").astimezone(_CST).strftime("%Y-%m-%d")
    except ValueError:
        pass

    # ISO 8601 with tz
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_CST).strftime("%Y-%m-%d")
    except ValueError:
        pass

    now = _today()

    if "刚刚" in s or "秒前" in s:
        return now.strftime("%Y-%m-%d")

    m = re.match(r"^(\d+)\s*分钟前", s)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).strftime("%Y-%m-%d")

    m = re.match(r"^(\d+)\s*小时前", s)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d")

    if s.startswith("今天"):
        return now.strftime("%Y-%m-%d")

    if s.startswith("昨天"):
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # MM-DD (implicit same year on Weibo mobile)
    m = re.match(r"^(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            dt = datetime(now.year, int(m.group(1)), int(m.group(2)), tzinfo=_CST)
            # If parsed date is in the future (e.g. Jan post seen in Dec), roll back a year.
            if dt > now + timedelta(days=1):
                dt = dt.replace(year=now.year - 1)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    # YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=_CST).strftime("%Y-%m-%d")
        except ValueError:
            return None

    return None


def _in_window(date_str: Optional[str], from_date: str, to_date: str) -> bool:
    if not date_str:
        return True
    if from_date and date_str < from_date:
        return False
    if to_date and date_str > to_date:
        return False
    return True


def _mblog_overlap(topic: str, text: str) -> float:
    if not text:
        return 0.0
    return token_overlap_relevance(topic, text)


def _fetch_search(keyword: str, page: int, realtime: bool, config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Fetch one search page; returns raw card list (may be empty).

    ``realtime=True`` hits the 实时 tab (containerid type 61) which is a
    stronger recency signal; False hits the 综合 tab (type 1) which is
    broader-quality-ranked.
    """
    type_code = "61" if realtime else "1"
    # containerid must have ``type=<n>`` URL-encoded as ``type%3D<n>``
    container = urllib.parse.quote(f"100103type={type_code}&q={keyword}", safe="")
    url = (
        f"https://m.weibo.cn/api/container/getIndex"
        f"?containerid={container}&page_type=searchall&page={page}"
    )
    data = _get_json(url, config=config)
    if not isinstance(data, dict):
        raise http.HTTPError(f"Weibo search returned unexpected shape: {type(data).__name__}")
    if data.get("ok") != 1:
        return []
    cards = ((data.get("data") or {}).get("cards")) or []
    if not isinstance(cards, list):
        return []
    return cards


def _iter_mblogs(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Walk cards + card_group to extract mblog payloads (card_type=9)."""
    out: List[Dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        if card.get("card_type") == 9 and isinstance(card.get("mblog"), dict):
            out.append(card["mblog"])
        group = card.get("card_group") or []
        if isinstance(group, list):
            for sub in group:
                if isinstance(sub, dict) and sub.get("card_type") == 9 and isinstance(sub.get("mblog"), dict):
                    out.append(sub["mblog"])
    return out


def _normalize_mblog(mblog: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a raw Weibo mblog to the web-item shape."""
    if not isinstance(mblog, dict):
        return None
    mid = mblog.get("id") or mblog.get("mid")
    if not mid:
        return None

    user = mblog.get("user") or {}
    raw_text = mblog.get("longText", {}).get("longTextContent") if isinstance(mblog.get("longText"), dict) else None
    text = _strip_html(raw_text or mblog.get("text") or "")
    created = _parse_created_at(mblog.get("created_at"))

    likes = int(mblog.get("attitudes_count") or 0)
    comments = int(mblog.get("comments_count") or 0)
    reposts = int(mblog.get("reposts_count") or 0)

    author = user.get("screen_name") or user.get("name") or ""
    author_id = user.get("id") or user.get("idstr") or ""

    return {
        "id": str(mid),
        "title": text[:60] or "微博",
        "url": f"https://m.weibo.cn/detail/{mid}",
        "source_domain": "weibo.cn",
        "snippet": text[:300],
        "body": text,
        "date": created,
        "date_confidence": "high" if created else "low",
        "relevance": 0.0,
        "why_relevant": "",
        "engagement": {
            "likes": likes,
            "comments": comments,
            "reposts": reposts,
        },
        "author": author,
        "author_id": str(author_id) if author_id else "",
        "mblog_id": str(mid),
    }


def search_weibo(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Search Weibo for statuses related to ``topic``.

    Returns ``{"results": [...]}`` with normalized web-item dicts. On
    transport/parse failure returns ``{"results": [], "error": "..."}``.
    A missing cookie is a configuration error, not a quiet empty state.
    """
    if not topic or not topic.strip():
        return {"results": []}

    limit = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])

    if not (config or {}).get(COOKIE_CONFIG_KEY):
        return {"results": [], "error": f"{COOKIE_CONFIG_KEY} not configured"}

    keyword = topic.strip()
    raw_items: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _absorb(mblogs: List[Dict[str, Any]]) -> None:
        for mblog in mblogs:
            status = _normalize_mblog(mblog)
            if not status:
                continue
            key = status["id"]
            if key in seen:
                continue
            seen.add(key)
            raw_items.append(status)

    # Two-stream fetch: 综合 (broad) + 实时 (recent). Cap pages so a huge
    # topic doesn't blow the budget; ``limit`` alone bounds the returned set.
    max_pages = 3 if depth == "deep" else 2 if depth == "default" else 1
    try:
        for page in range(1, max_pages + 1):
            cards = _fetch_search(keyword, page=page, realtime=False, config=config)
            _absorb(_iter_mblogs(cards))
            if not cards:
                break
        # 实时 tab is best-effort; failure here shouldn't kill 综合 results.
        try:
            for page in range(1, max_pages + 1):
                cards = _fetch_search(keyword, page=page, realtime=True, config=config)
                _absorb(_iter_mblogs(cards))
                if not cards:
                    break
        except Exception as exc:
            _log(f"realtime tab fetch failed: {exc}")
    except Exception as exc:
        _log(f"Weibo search failed: {exc}")
        return {"results": [], "error": str(exc)}

    results: List[Dict[str, Any]] = []
    for status in raw_items:
        if not status["body"]:
            continue
        if not _in_window(status["date"], from_date, to_date):
            continue
        score = _mblog_overlap(topic, status["body"])
        if score <= 0:
            continue
        status["relevance"] = score
        status["why_relevant"] = (
            f"Weibo status token overlap with '{topic}' (score {score:.2f})"
        )
        results.append(status)

    results.sort(
        key=lambda it: (
            it["relevance"],
            it["engagement"].get("likes", 0)
            + it["engagement"].get("reposts", 0)
            + it["engagement"].get("comments", 0),
        ),
        reverse=True,
    )
    _log(f"query '{topic}' -> {len(results)} relevant statuses (from {len(raw_items)} raw)")
    return {"results": results[:limit]}


def parse_weibo_response(result: Any, query: str = "") -> List[Dict[str, Any]]:
    """Parse a ``search_weibo`` envelope into the pipeline's item shape."""
    if not isinstance(result, dict):
        return []
    results = result.get("results") or []
    if not isinstance(results, list):
        return []
    parsed: List[Dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        parsed.append({
            "id": item.get("id") or "",
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "author": item.get("author") or "",
            "snippet": item.get("snippet") or "",
            "body": item.get("body") or "",
            "date": item.get("date"),
            "date_confidence": item.get("date_confidence", "low"),
            "relevance": item.get("relevance", 0.0),
            "why_relevant": item.get("why_relevant") or "",
            "engagement": item.get("engagement") or {},
            "container": "微博",
            "metadata": {
                "mblog_id": item.get("mblog_id"),
                "author_id": item.get("author_id"),
            },
        })
    return parsed
