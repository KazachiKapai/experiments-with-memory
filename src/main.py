import json
import uuid
import time
import logging
import sys
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from src.models import (
    Citation,
    HealthResponse,
    Memory,
    RecallRequest,
    RecallResponse,
    SearchRequest,
    SearchResponse,
    SearchResult,
    TurnRequest,
    TurnResponse,
    UserMemoriesResponse,
)
from src.database import (
    init_db,
    store_facts,
    get_user_conversation_history,
    create_turn,
    search_facts,
    get_user_memories,
    delete_session,
    delete_user,
    conn
)
from src.llm import (
    extract_facts_from_history,
    extract_implicit_facts,
    count_tokens
)
from src.search import (
    unified_search,
)
from src.logic import (
    update_user_doc_with_history,
    truncate_to_max_tokens,
    extract_facts_fallback,
)

# --- App Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Memory Service", version="0.2.0")

# --- Langfuse Integration ---
langfuse_client = None


def get_langfuse():
    global langfuse_client
    if langfuse_client is None:
        try:
            from langfuse import get_client
            langfuse_client = get_client()
        except ImportError:
            logger.warning("Langfuse not installed")
            langfuse_client = False
        except Exception as e:
            logger.warning(f"Langfuse init failed: {e}")
            langfuse_client = False
    return langfuse_client if langfuse_client else None

from src.errors import APIError

@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    logger.error(f"API Error: {exc.message} | Details: {exc.details}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message, "details": exc.details, "path": str(request.url.path)}
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "message": str(exc), "path": str(request.url.path)}
    )

# --- Middleware ---
class TracingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = str(uuid.uuid4())[:8]
        request.state.trace_id = trace_id
        start_time = time.time()
        
        logger.info(f"[{trace_id}] {request.method} {request.url.path}")
        
        request_body = ""
        if request.method in ["POST", "PUT", "PATCH"]:
            try:
                body = await request.body()
                request_body = body.decode('utf-8')[:500] if body else ""
            except:
                pass
        
        lf = get_langfuse()
        if lf:
            try:
                with lf.start_as_current_observation(as_type="span", name=f"{request.method} {request.url.path}") as span:
                    request.state.langfuse_span = span
                    if request_body:
                        span.update(input=request_body)
                    
                    response = await call_next(request)
                    
                    try:
                        body = b""
                        async for chunk in response.body_iterator:
                            body += chunk
                        
                        response_text = body.decode('utf-8')[:1000] if body else ""
                        span.update(output=response_text if response_text else {"status": response.status_code})
                        
                        return StarletteResponse(
                            content=body,
                            status_code=response.status_code,
                            headers=dict(response.headers),
                            media_type=response.media_type
                        )
                    except Exception:
                        span.update(output={"status": response.status_code})
            except Exception as e:
                logger.error(f"Langfuse error: {e}")
                response = await call_next(request)
        else:
            response = await call_next(request)
        
        duration = time.time() - start_time
        logger.info(f"[{trace_id}] {response.status_code} | {duration:.3f}s")
        
        return response

app.add_middleware(TracingMiddleware)

# --- Database Initialization ---
init_db()

# --- Endpoints ---
@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok")

@app.post("/turns", response_model=TurnResponse, status_code=201)
async def post_turn(req: TurnRequest):
    try:
        turn_id = str(uuid.uuid4())
        create_turn(turn_id, req)

        if req.user_id:
            try:
                full_history = get_user_conversation_history(req.user_id)
                messages = [m.model_dump() for m in req.messages]
                facts = await extract_facts_from_history(full_history, req.user_id, turn_id)

                if not facts:
                    facts = extract_facts_fallback(messages)
                    logger.info(f"Fallback extracted {len(facts)} facts")

                if facts:
                    store_facts(facts, req.user_id, req.session_id, turn_id)
                    await update_user_doc_with_history(req.user_id)

                implicit_facts = await extract_implicit_facts(req.user_id, req.session_id)
                if implicit_facts:
                    store_facts(implicit_facts, req.user_id, req.session_id, turn_id)
                    logger.info(f"Implicit agent stored {len(implicit_facts)} pattern facts")
            except Exception as e:
                logger.error(f"Fact extraction error: {e}")

        return TurnResponse(id=turn_id)
    except Exception as e:
        logger.exception(f"Failed to create turn: {e}")
        raise APIError("Failed to create turn", status_code=500, details={"error": str(e)})

@app.post("/recall", response_model=RecallResponse)
async def recall(req: RecallRequest):
    user_id = req.user_id or "unknown"
    try:
        top_chunks, search_type = unified_search(req.query, user_id, top_k=5)
        
        if top_chunks:
            context = "\n\n".join([f"## {c['section']}\n{c['content']}" for c in top_chunks[:3]])
            if req.max_tokens and count_tokens(context) > req.max_tokens:
                context = truncate_to_max_tokens(context, req.max_tokens)
            
            citations = [
                Citation(
                    turn_id=c.get("id", ""), 
                    score=c.get("score", 0.9 if search_type == "semantic" else 0.5), 
                    snippet=c["content"][:200]
                ) for c in top_chunks[:3]
            ]
            logger.info(f"Recall ({search_type}): {len(top_chunks)} chunks, top score: {top_chunks[0].get('score', 0):.2f}")
            return RecallResponse(context=context, citations=citations)

        # Fallback to last turn if search fails
        cur = conn.execute("SELECT messages FROM turns WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (user_id,))
        turn_result = cur.fetchone()
        context = ""
        if turn_result:
            try:
                msgs = json.loads(turn_result["messages"])
                context = "\n".join([f'{m.get("role", "user").upper()}: "{m["content"]}"' for m in msgs if m.get("content")])
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to parse recent messages: {e}")
        
        if req.max_tokens and count_tokens(context) > req.max_tokens:
            context = truncate_to_max_tokens(context, req.max_tokens)
            
        return RecallResponse(context=context, citations=[])
    except Exception as e:
        logger.exception(f"Recall endpoint failed: {e}")
        raise APIError("Failed to recall memories", status_code=500, details={"error": str(e)})

@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    try:
        results = search_facts(req.query, req.user_id or "%", req.session_id or "%", req.limit)
        return SearchResponse(results=[
            SearchResult(
                content=row["value"],
                score=row["score"],
                session_id=row["session_id"] or "",
                timestamp=row["timestamp"] or "",
                metadata={"key": row["key"]},
            ) for row in results
        ])
    except Exception as e:
        logger.exception(f"Search failed: {e}")
        raise APIError("Search failed", status_code=500, details={"error": str(e)})

@app.get("/users/{user_id}/memories", response_model=UserMemoriesResponse)
async def get_user_memories_endpoint(user_id: str):
    try:
        rows = get_user_memories(user_id)
        memories = [
            Memory(
                id=row["id"],
                type=row["type"],
                key=row["key"],
                value=row["value"],
                confidence=row["confidence"],
                source_session=row["session_id"],
                source_turn=row["turn_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                supersedes=row["supersedes"],
                active=bool(row["active"]),
            ) for row in rows
        ]
        return UserMemoriesResponse(memories=memories)
    except Exception as e:
        logger.exception(f"Failed to get user memories: {e}")
        raise APIError("Failed to retrieve memories", status_code=500, details={"error": str(e), "user_id": user_id})

@app.delete("/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    try:
        delete_session(session_id)
        logger.info(f"Deleted session: {session_id}")
        return Response(status_code=204)
    except Exception as e:
        logger.exception(f"Failed to delete session {session_id}: {e}")
        raise APIError("Failed to delete session", status_code=500, details={"error": str(e), "session_id": session_id})

@app.delete("/users/{user_id}")
async def delete_user_endpoint(user_id: str):
    try:
        delete_user(user_id)
        logger.info(f"Deleted user: {user_id}")
        return Response(status_code=204)
    except Exception as e:
        logger.exception(f"Failed to delete user {user_id}: {e}")
        raise APIError("Failed to delete user", status_code=500, details={"error": str(e), "user_id": user_id})
