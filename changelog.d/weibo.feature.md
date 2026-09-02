feat: add Weibo (微博) Chinese microblog source

- `weibo` source: pulls statuses from Weibo's public mobile search
  endpoint (`m.weibo.cn/api/container/getIndex`, both 综合 and 实时
  tabs). General-purpose, no topic gate.
- Zero-config credentials: on first call the adapter auto-acquires a
  visitor pass cookie via `passport.weibo.com/visitor/genvisitor` +
  `visitor/visitor?a=incarnate`, following the same two-step JSONP flow
  Weibo's own web login uses for anonymous browsers. `WEIBO_COOKIE` is
  optional — supplying a logged-in browser cookie overrides the visitor
  pass and gets higher rate limits.
- Follows the same listing-adapter pattern as `v2ex` / `xueqiu`:
  normalized web-item shape, hard date filter, token-overlap relevance
  gate, engagement (attitudes / comments / reposts) in
  `SourceItem.engagement`. Author screen name and mblog id ride along in
  metadata.
- Weibo timestamps are heterogeneous ("Mon Sep 01 12:34:56 +0800 2025",
  "刚刚", "N分钟前", "昨天 HH:MM", "MM-DD", "YYYY-MM-DD"); the parser
  normalizes all of them to CST YYYY-MM-DD so the date window is
  reliable.
- `available_sources()` registers `weibo` unconditionally (mirrors
  `v2ex` / `hackernews`). Disable with `EXCLUDE_SOURCES=weibo` when
  visitor-pass generation is unwanted.
- Tests: `tests/test_weibo.py` (pure-logic unit tests; network calls +
  visitor-pass generation mocked). `KNOWN_SOURCE_NAMES` updated.
