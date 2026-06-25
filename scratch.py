from app.community.store import _supabase
try:
    res = _supabase().rpc(
        "match_knowledge_chunks",
        {
            "query_embedding": [0.0]*384,
            "match_threshold": -1.0,
            "match_count": 1
        }
    ).execute()
    print("RPC with zeroes:", res.data)
except Exception as e:
    import traceback
    traceback.print_exc()
