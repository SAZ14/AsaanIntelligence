# Deploying the Asaan Intelligence WhatsApp Webhook

This deploys the FastAPI inbound-WhatsApp agent to [Render](https://render.com)
so Twilio's WhatsApp Sandbox has a **stable public URL** to send inbound
messages to — no local `ngrok` required.

## What gets deployed

- **App entrypoint:** `app.whatsapp_webhook:app`
- **Routes:**
  - `GET  /health` → `{"status": "ok"}` (used by Render's health check)
  - `GET  /` → basic status JSON
  - `POST /whatsapp/incoming` → Twilio inbound webhook (form-encoded)
- **Start command:** `uvicorn app.whatsapp_webhook:app --host 0.0.0.0 --port $PORT`

When an inbound WhatsApp message contains **"bad comments", "bad reviews",
"check instagram", "anatummy", or "instagram"**, the agent scrapes
`https://www.instagram.com/anatummyisb/` via Apify, analyzes the posts with
Claude (`ANTHROPIC_API_KEY`), and sends back a WhatsApp-ready report (under
1500 characters). Because the scrape takes ~30–60s (longer than Twilio's ~15s
webhook timeout), the webhook replies instantly with an acknowledgement and
then pushes the finished report back as a separate outbound WhatsApp message.
Any other message gets an immediate echo reply.

## Environment variables

Set these in Render (dashboard **Environment** tab, or you'll be prompted for
the `sync: false` ones on first deploy). **Never commit the secret values.**

| Variable | Notes |
| --- | --- |
| `APIFY_TOKEN` | **secret** — Apify API token |
| `ANTHROPIC_API_KEY` | **secret** — Claude API key (`sk-ant-...`) |
| `TWILIO_ACCOUNT_SID` | **secret** — Twilio Account SID (`AC...`) |
| `TWILIO_AUTH_TOKEN` | **secret** — Twilio Auth Token |
| `TWILIO_WHATSAPP_NUMBER` | `whatsapp:+14155238886` (Twilio sandbox sender) |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` (fallback sender) |
| `OWNER_NUMBER` | `whatsapp:+16292595668` (report recipient) |
| `TWILIO_WHATSAPP_TO` | `whatsapp:+16292595668` (fallback recipient) |
| `DRY_RUN` | `0` (send real messages; `1` = print only) |

The four secrets are declared `sync: false` in `render.yaml`, so their values
are entered in the Render dashboard rather than stored in the repo. The
non-secret values above are already set in `render.yaml`.

## Deploy on Render

You can deploy either from the `render.yaml` blueprint (recommended) or by
configuring a service manually.

### Option A — Blueprint (uses `render.yaml`)

1. Push this repo to GitHub (already done if you're reading this on the PR).
2. In Render, click **New → Blueprint**.
3. Connect the GitHub repo and select this branch.
4. Render reads `render.yaml` and proposes the `asaan-whatsapp-webhook`
   web service. Click **Apply**.
5. When prompted, paste the four secret values (`APIFY_TOKEN`,
   `ANTHROPIC_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`).
6. Wait for the build + deploy to finish (status **Live**).

### Option B — Manual web service

1. In Render, click **New → Web Service** and connect this repo/branch.
2. Set:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.whatsapp_webhook:app --host 0.0.0.0 --port $PORT`
   - **Health Check Path:** `/health`
3. Under **Environment**, add every variable from the table above.
4. Click **Create Web Service** and wait for **Live**.

## Verify the deployment

Once live, Render gives you a public URL like
`https://asaan-whatsapp-webhook.onrender.com`. Check health:

```bash
curl https://YOUR-RENDER-URL/health
# {"status":"ok"}
```

## Point Twilio at the deployed URL

1. Open the [Twilio Console](https://console.twilio.com) →
   **Messaging → Try it out → Send a WhatsApp message → Sandbox settings**.
2. In **"When a message comes in"**, paste:

   ```
   https://YOUR-RENDER-URL/whatsapp/incoming
   ```

3. Set the method to **POST**.
4. Click **Save**.

> Replace `YOUR-RENDER-URL` with your actual Render hostname, e.g.
> `https://asaan-whatsapp-webhook.onrender.com/whatsapp/incoming`.

## Test it

From the phone joined to your Twilio WhatsApp sandbox:

- Send `hello` → you get `Asaan Intelligence received: hello`.
- Send `Any bad comments?` → you get an instant acknowledgement, then the
  full Instagram reputation report arrives as a second WhatsApp message.

## Notes

- **Free plan cold starts:** Render's free web services sleep after inactivity
  and take ~30–60s to wake. The first inbound message after idle may be slow;
  `/health` is polled by Render to keep the service monitored.
- **24-hour window:** Business-initiated WhatsApp messages only deliver if the
  recipient messaged the sandbox within the last 24h. Since the report is
  triggered *by* an inbound message, you're always within the window.
