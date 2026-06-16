"""Run the multi-café AsaanPay Revenue Advisor (one bot for all owners).

    python -m scripts.revenue_multi_serve

Point the single Twilio WhatsApp number's inbound webhook at:
    POST  https://<your-host>/webhook/whatsapp
Trigger scheduled digests across all cafés from cron:
    POST  https://<your-host>/digests/weekly
See app/revenue/multi_api.py for environment variables.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("REVENUE_HOST", "0.0.0.0")
    port = int(os.environ.get("REVENUE_PORT", "8002"))
    uvicorn.run("app.revenue.multi_api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
