"""Run the Revenue agent WhatsApp webhook service.

    python -m scripts.revenue_serve

Point your Twilio WhatsApp sandbox inbound webhook at:
    POST  https://<your-host>/webhook/whatsapp
Trigger scheduled digests from cron:
    POST  https://<your-host>/digests/weekly
See app/revenue/api.py for the environment variables it reads.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("REVENUE_HOST", "0.0.0.0")
    port = int(os.environ.get("REVENUE_PORT", "8001"))
    uvicorn.run("app.revenue.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
