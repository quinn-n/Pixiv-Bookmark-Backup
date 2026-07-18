# TODO

Non-blocking follow-ups from the review board on the bookmark-pagination
token-expiry fix (`Pixivpy3Client._fetch_page` in `pixiv_client.py`). None of
these block that change from shipping.

## From quality review

- [ ] Add a dedicated re-auth regression test for `iter_user_works`
  (mirroring `test_iter_bookmarks_reauths_and_continues_on_expired_token` /
  `test_iter_following_reauths_and_continues_on_expired_token`). It shares the
  same `_fetch_page` call sites as the other two generators but has no test
  exercising the error/recovery path.
- [x] `_fetch_bookmark_tags` (`pixiv_client.py`) had the same latent defect
  this fix addressed: it called `illust_bookmark_detail` directly, not through
  `_fetch_page`, so a token expiry between pages made bookmark tags silently
  come back `None` instead of recovering. Now routed through `_fetch_page`
  (re-auth-with-backoff). Because losing one work's personal tags is not worth
  aborting a large run, `_fetch_bookmark_tags` catches `_fetch_page`'s
  exhausted-budget `ApiError` and degrades to `None` for just that work *after*
  the bounded re-auth/retry ran, surfacing a visible per-work `warn` (a
  systemic token failure is still backstopped by the next listing page's own
  `_fetch_page`). Regression tests in `tests/test_pixiv_client.py`:
  `test_bookmark_tags_reauth_and_recovers_on_expired_token` (recovery succeeds)
  and `test_bookmark_tags_degrade_to_none_and_warn_when_error_persists`
  (graceful give-up + warn + bounded re-auth still attempted).

## From security review

- [x] Mid-pagination re-auth failures inside `_fetch_page` propagate
  `pixivpy3`'s raw `PixivError` (which embeds the auth endpoint's HTTP
  response text) into the resulting `ApiError` message, unlike `authenticate()`
  which deliberately raises a static `AuthRejected` message to avoid surfacing
  that text. **Done:** the retry loop now catches the re-auth `PixivError` and,
  once the retry budget is exhausted, raises a static `ApiError` that names the
  failure kind ("a network error" vs "an error response") without embedding the
  underlying exception text -- mirroring `authenticate()`'s convention.
- [x] `_fetch_page`'s same-page retry fetch fires immediately with no
  politeness delay, unlike the `self._sleep(self._delay)` pause applied
  between distinct pages. **Done:** resolved as a side effect of adding
  exponential-backoff-with-jitter (`self._sleep(...)`) before each recovery
  re-auth, so retries are now paced rather than firing back-to-back.
- [ ] `pip-audit` currently reports 9 known CVEs in `pillow` (12.2.0, fixed in
  12.3.0) and `setuptools` (82.0.1, fixed in 83.0.0). Unrelated to this change;
  needs a separate dependency-bump task.
