# Sources — terms of service and access approach

One entry per source. Record what the source's terms say about automated access,
the access approach chosen, and the date the terms were last reviewed. If a
source's terms clearly prohibit automated access, it is **flagged and not
implemented** — the correct move there is to pursue an official affiliate or
partner feed, not to scrape.

Crawling policy applies to every implemented source:

- Honour `robots.txt`.
- Identify with a descriptive User-Agent including a contact URL.
- Rate-limit conservatively (default: no faster than 1 request / 3 seconds,
  per source, configurable in `config/sources.yaml`).
- Exponential backoff with jitter on `429`/`5xx`.
- **Never** use residential proxy rotation or CAPTCHA-solving services. If a
  source requires them, that is a signal to stop.

---

## Shopify storefront JSON — `shopify` adapter

- **Access approach:** Fetch each store's public `/products.json` endpoint
  (paginated). This is structured JSON that Shopify exposes publicly; no HTML
  scraping, no parsing of anti-bot markup, no JavaScript.
- **Per store:** `robots.txt` and the store's own terms must be checked and the
  store added to `config/sources.yaml` only after that check. `/products.json`
  being reachable is **not** on its own sufficient — some stores disable or
  throttle it, and a store's terms may still forbid automated collection.
- **Personal data:** Shopify product data is dealer/business inventory, not
  private-seller personal data. Still minimise what is stored.
- **Terms last reviewed:** _pending — populate per store before enabling it._
- **Status:** implemented (adapter); stores enabled individually in config.

> Open question being resolved separately: which target dealers actually expose
> a usable `/products.json`. Until confirmed per store, the shopify source ships
> with example/placeholder stores disabled by default.

---

## eBay APIs — `ebay` adapter

- **Access approach:** Official eBay Developer Program APIs (Browse API for live
  listings; Marketplace Insights for sold data where the access tier is
  granted). No scraping.
- **Status:** **stub / unconfigured.** The adapter is registered so the
  interface is exercised, but until developer-program credentials and the
  required access tier are confirmed, its `health_check()` reports
  `unconfigured` and the crawl **skips** it (this is not a failure state).
- **Terms:** eBay API use is governed by the eBay Developers Program agreement;
  review and record the relevant terms before enabling.
- **Terms last reviewed:** _pending._

---

## Not implemented in Phase 0

- **Auction houses, Reddit, non-Shopify dealer HTML** — deferred to later phases
  by design; add here with their terms when built.
- **Chrono24** — terms prohibit automated access and it runs active bot
  detection. **Flagged: do not scrape.** Pursue an affiliate/partner feed. Not
  implemented.
