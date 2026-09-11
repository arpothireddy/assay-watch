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
- **Status:** implemented (adapter); stores enabled individually below.

### Enabled stores

| Store | Base URL | `/products.json` | `robots.txt` | ToS reviewed |
|---|---|---|---|---|
| Windup Watch Shop | windupwatchshop.com | reachable | permits it | _pending_ |
| Topper Jewelers | topperjewelers.com | reachable | permits it | _pending_ |
| Analog:Shift | analogshift.com | reachable | permits it | _pending_ |
| Hodinkee Shop | shop.hodinkee.com | reachable | permits it | _pending_ |
| Craft + Tailored | craftandtailored.com | reachable | permits it | no explicit ban found |
| Bulang & Sons | bulangandsons.com | reachable | permits it | no explicit ban found |
| Collective Horology | collectivehorology.com | reachable | permits it | no explicit ban found |

`/products.json` and `robots.txt` were checked 2026-09-09 (first four) and
2026-09-11 (next three). "No explicit ban found" is a ToS skim, not a full
legal review — it means no automated-access prohibition was spotted, not that
a lawyer signed off. Flip a store back to `enabled: false` in
`config/sources.yaml` if a closer read turns up a problem. The four marked
`_pending_` haven't had even that skim yet.

Currency note: Bulang & Sons is UK-based, recorded as `GBP`. Collective
Horology's currency is unconfirmed and left unset in `config/sources.yaml`
rather than guessed.

### Checked and rejected

| Store | Base URL | Reason |
|---|---|---|
| Crown & Caliber | crownandcaliber.com | `/products.json` redirects/404s — not a live Shopify storefront endpoint |
| Teddy Baldassarre | teddybaldassarre.com | Cloudflare bot-challenge in front of the site — unreachable without defeating bot protection, which the crawling policy above rules out |

> Open question being resolved on an ongoing basis: which further target
> dealers expose a usable `/products.json`. New candidates get the same
> reachability + `robots.txt` + ToS check before joining the table above.

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
