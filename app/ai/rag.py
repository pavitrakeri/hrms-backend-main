import json
import logging
import math
from typing import List, Dict, Any, Optional
from app.ai.config import get_gemini_client, EMBEDDING_MODEL

logger = logging.getLogger(__name__)

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)

def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> List[str]:
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i : i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)
        i += chunk_size - overlap
    return chunks

async def get_embedding(text: str) -> Optional[List[float]]:
    client = get_gemini_client()
    if not client:
        return None
    try:
        res = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text
        )
        if hasattr(res, "embeddings") and res.embeddings:
            return res.embeddings[0].values
    except Exception as e:
        logger.error(f"Error generating embedding with Gemini: {e}")
    return None

async def index_all_policies(conn):
    """Indexes all policy descriptions and records into policy_embeddings table."""
    policies = await conn.fetch("SELECT id, title, description FROM policies")
    count = 0
    for p in policies:
        combined_text = f"Policy Title: {p['title']}\nDescription: {p['description'] or 'N/A'}"
        chunks = chunk_text(combined_text)
        for idx, chunk in enumerate(chunks):
            emb = await get_embedding(chunk)
            emb_json = json.dumps(emb) if emb else None
            await conn.execute("""
                INSERT INTO policy_embeddings (id, policy_id, chunk_index, content, embedding)
                VALUES (gen_random_uuid(), $1, $2, $3, $4)
            """, p["id"], idx, chunk, emb_json)
            count += 1
    logger.info(f"Indexed {count} policy chunks for RAG.")
    return count

async def search_policies(conn, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """Semantic RAG search over policies with keyword fallback."""
    query_emb = await get_embedding(query)
    results = []

    if query_emb:
        # Load embeddings from DB
        rows = await conn.fetch("""
            SELECT pe.content, pe.embedding, p.title, p.file_url
            FROM policy_embeddings pe
            JOIN policies p ON pe.policy_id = p.id
            WHERE pe.embedding IS NOT NULL
        """)
        scored = []
        for r in rows:
            try:
                emb = json.loads(r["embedding"])
                score = cosine_similarity(query_emb, emb)
                scored.append({
                    "title": r["title"],
                    "content": r["content"],
                    "file_url": r["file_url"],
                    "score": score
                })
            except Exception:
                continue
        scored.sort(key=lambda x: x["score"], reverse=True)
        results = scored[:top_k]

    # If no embedding match or embeddings not yet indexed, fallback to ILIKE text search
    if not results or (results and results[0]["score"] < 0.2):
        fallback_rows = await conn.fetch("""
            SELECT title, description as content, file_url
            FROM policies
            WHERE title ILIKE $1 OR description ILIKE $1
            LIMIT $2
        """, f"%{query}%", top_k)
        if fallback_rows:
            results = [{
                "title": r["title"],
                "content": r["content"] or "",
                "file_url": r["file_url"],
                "score": 0.5
            } for r in fallback_rows]

    return results
