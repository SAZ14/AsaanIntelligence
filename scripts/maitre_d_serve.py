"""Run the Maître d' WhatsApp webhook service.

    python -m scripts.maitre_d_serve

Then point your Twilio WhatsApp sandbox's inbound webhook at:
    POST  https://<your-host>/webhook/whatsapp

See app/maitre_d/api.py for the environment variables it reads.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("MAITRE_D_HOST", "0.0.0.0")
    port = int(os.environ.get("MAITRE_D_PORT", "8000"))
    uvicorn.run("app.maitre_d.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
