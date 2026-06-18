#!/usr/bin/env python3
"""Start the Asaan Intelligence FastAPI server."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=8000, reload=True)
