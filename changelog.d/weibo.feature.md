feat: add Weibo (微博) Chinese microblog source

- `weibo` source: pulls statuses from Weibo's public mobile search
  endpoint (`m.weibo.cn/api/container/getIndex`, both 综合 and 实时
  tabs). General-purpose, no topic gate — activates for any topic when
  `WEIBO_COOKIE` is configured. Without the cookie the endpoint returns
  empty/redirect for anonymous callers, so the source stays unregistered.
- Follows the same listing-adapter pattern as `v2ex` / `xueqiu`:
  normalized web-item shape, hard date filter, token-overlap relevance
  gate, engagement (attitudes / comments / reposts) in
  `SourceItem.engagement`. Author screen name and mblog id ride along in
  metadata.
- Weibo timestamps are heterogeneous ("Mon Sep 01 12:34:56 +0800 2025",
  "刚刚", "N分钟前", "昨天 HH:MM", "MM-DD", "YYYY-MM-DD"); the parser
  normalizes all of them to CST YYYY-MM-DD so the date window is
  reliable.
- `available_sources()` registers `weibo` only when `WEIBO_COOKIE` is
  present, matching the Xueqiu gating pattern.
- Tests: `tests/test_weibo.py` (pure-logic unit tests; network calls
  mocked). `KNOWN_SOURCE_NAMES` updated.
