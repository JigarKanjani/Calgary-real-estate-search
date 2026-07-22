# Telegram Recipients — Calgary Listings Bot

This bot sends new Calgary resale-listing alerts to Telegram. Every recipient
is a numeric **chat ID**. You can add as many as you like by comma-separating
their IDs in a single GitHub secret.

---

## 1. Create the bot (one-time)

You can reuse the **same bot** as the job-hunt bot, or make a dedicated one.

1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot`, give it a display name and a username ending in `bot`.
3. BotFather replies with a **token** like `123456789:AAExxxx...`.
4. Put that token in the repo secret **`TELEGRAM_BOT_TOKEN`**
   (Settings → Secrets and variables → Actions → New repository secret).

> If you reuse the job-hunt bot's token, `TELEGRAM_BOT_TOKEN` is already set.

---

## 2. Get a recipient's chat ID

⚠️ A bot cannot message someone who has never messaged it first. Each new
recipient must open the bot and tap **Start**.

**Easiest:** DM **@userinfobot** in Telegram — it replies with your numeric ID,
which is your 1:1 chat ID.

**For a group:** add the bot to the group, send a message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and read the negative
`"chat":{"id":-100...}` value.

---

## 3. Add the recipient

| Secret | Who receives what |
|---|---|
| `TELEGRAM_CHAT_ID` | default / fallback recipient (shared with job-hunt bot) |
| `TELEGRAM_CHAT_ID_LISTINGS` | recipients for **Calgary listings** (comma-separated) |

Set `TELEGRAM_CHAT_ID_LISTINGS` to send listings to a specific person/group
without touching the job-hunt recipients, e.g.:

```
747174717,123456789,-1001234567890
```

If `TELEGRAM_CHAT_ID_LISTINGS` is unset, the bot falls back to `TELEGRAM_CHAT_ID`.

---

## 4. Required for GitHub Actions — get past Realtor.ca's bot protection

Realtor.ca sits behind Imperva/Incapsula and **blocks GitHub's datacenter
IPs** (runs will log `[Realtor BLOCKED]` and return 0 listings). Pick **one**
of the options below. A managed scraper is recommended — it routes the request
through residential IPs that clear the block for you.

The bot auto-detects which option you configured (in this order): ScrapingBee →
Scrape.do → generic proxy → direct. Force one with the `SCRAPER_PROVIDER`
secret (`scrapingbee` | `scrapedo` | `direct`).

### Option A — ScrapingBee (recommended)

1. Sign up at [scrapingbee.com](https://www.scrapingbee.com) (free trial credits).
2. Copy your API key from the dashboard.
3. Add the secret **`SCRAPINGBEE_API_KEY`** = your key.

Uses `premium_proxy` (residential) + `country_code=ca` automatically.

### Option B — Scrape.do

1. Sign up at [scrape.do](https://scrape.do) (free monthly credits).
2. Copy your token.
3. Add the secret **`SCRAPEDO_TOKEN`** = your token.

Uses `super` (residential) mode + `geoCode=ca` automatically.

### Option C — your own residential proxy

Add the secret **`REALTOR_PROXY`** = `http://user:pass@proxy-host:port`.

> Running locally from a home/residential IP usually works with **none** of
> these set (direct connection). The scraper is mainly for the cloud runner.

**Watch your credits:** each 6-hour run makes a handful of requests (one per
result page). That's well within free tiers, but a managed scraper is a metered
service — check your provider dashboard occasionally.

---

## Notes

- Price window and property types are configured in `listing_alert.py`
  (`LISTING_PRICE_MIN/MAX`, `WANTED_TYPES`) or via the workflow env vars.
- Changes take effect on the next scheduled run (every 6 hours) or when you
  manually trigger the **Calgary Listings** workflow from the Actions tab.
