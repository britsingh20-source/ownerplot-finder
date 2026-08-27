# OwnerPlot Finder

GitHub Actions and Telegram discovery of publicly advertised owner plots across Coimbatore.

Example messages:

- `Search owner plots in Kalapatti`
- `Find plots in Saravanampatti under 60 lakhs`
- `/plots Kalapatti`
- `/watch plots Kalapatti` — alert only when a new public owner listing appears
- `/watches` — list active locality watches
- `/unwatch Kalapatti` — stop a locality watch

The bot searches configured, permitted sources; keeps plot listings; validates the locality; classifies owner vs broker; merges duplicates; and returns the public contact number, source URL, price, area, age and confidence.

Automatic watches run every `WATCH_INTERVAL_MINUTES` (60 by default). The first run records a baseline without flooding Telegram; later runs notify only unseen fingerprints. Telegram chat and user IDs are captured from `/watch`. Set `ALLOWED_TELEGRAM_USER_IDS` to keep the bot private.

## Non-negotiable source policy

OwnerPlot Finder does not bypass OTPs, CAPTCHAs, logins, paywalls, masked numbers, or `View Number` controls. A phone number is stored only when it is openly published or supplied by an authorized feed/user export. Each connector is explicitly classified as `allowed`, `authorized_only`, `manual_import`, or `disabled`.

99acres and MagicBricks must use an authorized feed or a user-initiated export/import. Their automated connectors are disabled by default.

## Architecture

```text
Telegram -> Query parser -> Search job -> Source registry -> Collectors
                                                    |
                                                    v
Results <- Telegram formatter <- Deduplication <- Normalization
                                           <- Locality validation
                                           <- Plot-only filter
                                           <- Owner classifier
                                           <- Public-contact policy
```

## GitHub Actions activation

Add repository secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `TAVILY_API_KEY`. The reviewed-domain defaults are stored in `config/allowed-public-domains.txt`; an `ALLOWED_PUBLIC_DOMAINS` repository variable can override them with a comma-separated list. GitHub supplies `GITHUB_TOKEN` automatically.

`monitor.yml` rotates one locality/source profile hourly: public social contacts, property portals, then local Coimbatore sites. A complete deep cycle across the locality registry takes about five days and remains within Tavily's 1,000 monthly free credits. Telegram-requested searches run all three profiles immediately, merge duplicates and normally reply within five minutes.

MagicBricks, 99acres, NoBroker and Housing are discovery-only: their public listing links and metadata may be returned, but OwnerPlot Finder never treats hidden contact controls as public phone evidence.

Every search applies a strict 90-day Tavily publish/last-updated cutoff. Older pages are excluded before owner classification and Telegram delivery.

## Evidence-based owner verification

Owner labels are not trusted by themselves. Each result records the original public post/update date, date confidence, seller advertisement history, broker-risk score, contact-verification level, and supporting evidence.

- One public advertisement claiming `direct owner` is only `probable_owner_call_to_confirm`.
- `property_matched_public_contact` requires the same property and public number on at least two different sources.
- A phone connected to five or more distinct advertisements receives elevated broker risk.
- Histories containing 25 or more advertisements are classified as broker/promoter even when the text claims direct ownership.
- Listings with a proven date older than 90 days are excluded. Listings without a provable original date are labelled unverified in Telegram instead of being presented as recent.

## Local quick start

1. Copy `.env.example` to `.env` and add the Telegram token.
2. Configure only sources for which you have permission.
3. Run `docker compose up --build`.
4. During local development, run polling with `python -m ownerplot.bot`.

### Activate public search

Create a Google Programmable Search Engine and set `GOOGLE_CSE_API_KEY` and `GOOGLE_CSE_ID`. Add only reviewed domains that permit collection to `ALLOWED_PUBLIC_DOMAINS`. With no reviewed domains, the system intentionally returns no contacts.

The collector requires HTTPS, a public-network destination, the reviewed domain allowlist and robots.txt permission. A number is returned only when visibly present in the fetched public page.

## Telegram output

```text
OWNER PLOTS — KALAPATTI
Found 7 unique plots; 3 have public owner contacts.

1. Residential plot · 2.5 cents · ₹42 lakh
Owner confidence: 91% · Posted 2 days ago
Contact: +91XXXXXXXXXX (public on source)
Source: https://...
```

## Source onboarding checklist

Before enabling a collector, record its terms URL, robots status, collection method, permission evidence, rate limit, fields permitted for storage, and retention period in `config/sources.yaml`. Unknown sources fail closed.

## Development

```bash
python -m unittest discover -s tests
```
