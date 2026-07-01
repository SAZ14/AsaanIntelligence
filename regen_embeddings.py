import json
from app.core.db import SessionLocal
from sqlalchemy import text
from app.agents.customer.community.store import _embedding_model

model = _embedding_model()
print("Model loaded:", model is not None)

with SessionLocal() as db:
    rows = db.execute(text("SELECT id, content FROM knowledge_base WHERE embedding IS NULL")).fetchall()
    print(f"Found {len(rows)} chunks missing embeddings")
    for row in rows:
        emb = model.encode(row[1]).tolist()
        db.execute(
            text("UPDATE knowledge_base SET embedding = CAST(:e AS vector) WHERE id = :id"),
            {"e": json.dumps(emb), "id": row[0]}
        )
        print(f"  Embedded {row[0][:8]}")
    db.commit()
    print("Done.")
