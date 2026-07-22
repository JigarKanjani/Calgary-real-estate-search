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

## 4. Optional — proxy for Realtor.ca

Realtor.ca sits behind Imperva bot protection and may block GitHub's
datacenter IPs. If runs report `[Realtor BLOCKED]`, set the secret
**`REALTOR_PROXY`** to a residential/rotating proxy URL:

```
http://user:pass@proxy-host:port
```

Leave it empty to connect directly (works from most residential IPs).

---

## Notes

- Price window and property types are configured in `listing_alert.py`
  (`LISTING_PRICE_MIN/MAX`, `WANTED_TYPES`) or via the workflow env vars.
- Changes take effect on the next scheduled run (every 6 hours) or when you
  manually trigger the **Calgary Listings** workflow from the Actions tab.
