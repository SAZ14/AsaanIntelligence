# Loyalty webhook — the always-on service Twilio calls on every QR scan.
# The webhook replies to customers via TwiML, so it needs only FastAPI +
# uvicorn at runtime (no Twilio SDK, no anthropic). SQLite is built into Python.
FROM python:3.11-slim

WORKDIR /srv

RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

COPY app ./app
COPY scripts ./scripts
COPY data ./data
COPY pyproject.toml ./

# Defaults. Override RESTAURANTS_CONFIG with your real venues file, and point
# LOYALTY_DB at a persistent disk so stamp counts survive restarts.
ENV RESTAURANTS_CONFIG=/srv/data/restaurants.example.json
ENV LOYALTY_DB=/data/loyalty.db

EXPOSE 8000

# $PORT is provided by most hosts (Render, Railway, ...); default to 8000 local.
CMD ["sh", "-c", "uvicorn app.whatsapp.webhook:app --host 0.0.0.0 --port ${PORT:-8000}"]
