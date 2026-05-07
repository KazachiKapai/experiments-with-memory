import math
import json
import logging
from typing import List, Dict, Optional

from rank_bm25 import BM25Okapi
from src.database import get_all_chunks_with_embeddings, get_all_chunks, count_user_embeddings, get_user_doc, delete_user_chunks, conn, get_chunks_to_embed, update_chunk_embedding
from src.llm import get_embedding
from src.logic import chunk_md_by_headers
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

_query_embedding_cache = {}


def get_cached_query_embedding(query: str) -> Optional[List[float]]:
    return _query_embedding_cache.get(query)


def set_cached_query_embedding(query: str, embedding: List[float]):
    _query_embedding_cache[query] = embedding


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def semantic_search_chunks(query: str, user_id: str, top_k: int = 5) -> List[Dict]:
    query_emb = get_cached_query_embedding(query)
    if not query_emb:
        try:
            query_emb = get_embedding(query)
            set_cached_query_embedding(query, query_emb)
        except Exception as e:
            logger.error(f"Embedding generation failed: {e}")
            raise

    rows = get_all_chunks_with_embeddings(user_id)
    
    results = []
    for row in rows:
        if row["embedding"]:
            try:
                chunk_emb = json.loads(row["embedding"])
                score = cosine_similarity(query_emb, chunk_emb)
                results.append({
                    "id": row["id"],
                    "section": row["section"],
                    "content": row["content"],
                    "score": score
                })
            except json.JSONDecodeError:
                continue
    
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


def bm25_search_chunks(query: str, user_id: str, top_k: int = 5) -> List[Dict]:
    rows = get_all_chunks(user_id)
    
    chunks = [{"id": r["id"], "section": r["section"], "content": r["content"]} for r in rows]
    if not chunks:
        return []
    
    corpus = [c["content"] for c in chunks]
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(query.split())
    
    results = [{**chunk, "score": scores[i]} for i, chunk in enumerate(chunks)]
    results.sort(key=lambda x: x["score"], reverse=True)
    
    return results[:top_k]


def ensure_user_embeddings(user_id: str) -> bool:
    if count_user_embeddings(user_id) > 0:
        return True
    
    logger.info(f"Generating embeddings for user {user_id}...")
    
    user_doc = get_user_doc(user_id)
    if user_doc and user_doc["full_doc"]:
        chunks = chunk_md_by_headers(user_doc["full_doc"])
        now = datetime.utcnow().isoformat() + "Z"
        
        delete_user_chunks(user_id)
        for chunk in chunks:
            conn.execute(
                "INSERT INTO md_chunks (id, user_id, section, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), user_id, chunk['section'], chunk['content'], now)
            )
        conn.commit()

    for row in get_chunks_to_embed(user_id):
        try:
            chunk_text = f"{row['section']}: {row['content']}"
            embedding = get_embedding(chunk_text)
            update_chunk_embedding(row["id"], json.dumps(embedding))
        except Exception as e:
            logger.warning(f"Embedding failed for chunk {row['id']}: {e}")
    
    logger.info(f"Embeddings generated for user {user_id}")
    return True

def unified_search(query: str, user_id: str, top_k: int = 5):
    ensure_user_embeddings(user_id)
    try:
        results = semantic_search_chunks(query, user_id, top_k)
        if results:
            return results, "semantic"
    except Exception as e:
        logger.warning(f"Semantic search failed: {e}")

    try:
        results = bm25_search_chunks(query, user_id, top_k)
        if results:
            return results, "bm25"
    except Exception as e:
        logger.error(f"BM25 fallback failed: {e}")

    return [], "none"
