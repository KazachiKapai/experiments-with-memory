import uuid
import json
import logging
from datetime import datetime
from typing import List

from src.database import (
    conn,
    get_user_doc,
    delete_user_chunks,
    insert_chunk,
    get_chunks_to_embed,
    update_chunk_embedding,
    get_all_user_conversations,
    get_active_facts,
    upsert_user_doc,
    count_user_embeddings,
)
from src.llm import get_embedding, count_tokens

logger = logging.getLogger(__name__)


def chunk_md_by_headers(md_text: str) -> List[dict]:
    if not md_text:
        return []
    
    chunks = []
    current_section = "General"
    content = []
    
    for line in md_text.split('\n'):
        if line.startswith('## '):
            if content:
                chunks.append({"section": current_section, "content": "\n".join(content).strip()})
            current_section = line[3:].strip()
            content = []
        else:
            content.append(line)
    
    if content:
        chunks.append({"section": current_section, "content": "\n".join(content).strip()})
    
    return chunks


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


async def update_user_doc_with_history(user_id: str):
    now = datetime.utcnow().isoformat() + "Z"
    
    sessions = get_all_user_conversations(user_id)
    if not sessions:
        return

    facts = get_active_facts(user_id)
    facts_by_key = {}
    for f in facts:
        key = f["key"]
        if key not in facts_by_key:
            facts_by_key[key] = []
        facts_by_key[key].append({"value": f["value"], "session_id": f["session_id"]})

    md_lines = [f"# User Profile: {user_id}", ""]
    sections = ["Employment", "Location", "Personal", "Hobbies", "Technology", "Preferences", "Other"]

    for section in sections:
        md_lines.append(f"## {section}")
        section_facts = []
        if section == "Employment":
            section_facts = facts_by_key.get("employment", []) + facts_by_key.get("job", [])
        elif section == "Location":
            section_facts = facts_by_key.get("location", [])
        elif section == "Personal":
            section_facts = facts_by_key.get("name", []) + facts_by_key.get("personal_info", [])

        if section_facts:
            for i, fact in enumerate(section_facts):
                is_current = " (current)" if i == len(section_facts) - 1 else ""
                md_lines.append(f"- [{fact['session_id']}] {fact['value']}{is_current}")
        else:
            md_lines.append("- (no data)")
        md_lines.append("")

    md_doc = "\n".join(md_lines)
    upsert_user_doc(user_id, md_doc, now)
    
    chunks = chunk_md_by_headers(md_doc)
    delete_user_chunks(user_id)
    
    for chunk in chunks:
        if chunk["content"].strip():
            chunk_id = str(uuid.uuid4())
            embedding_json = None
            try:
                embedding = get_embedding(f"{chunk['section']}: {chunk['content']}")
                embedding_json = json.dumps(embedding)
            except Exception as e:
                logger.warning(f"Embedding failed for chunk section {chunk['section']}: {e}")
            
            insert_chunk(chunk_id, user_id, chunk["section"], chunk["content"], embedding_json, now)
    
    logger.info(f"Updated MD for {user_id} with {len(sessions)} sessions and generated {len(chunks)} chunks.")


def truncate_to_max_tokens(context: str, max_tokens: int) -> str:
    if count_tokens(context) <= max_tokens:
        return context
    
    lines = context.split("\n")
    result = []
    for line in lines:
        if count_tokens("\n".join(result + [line])) > max_tokens:
            break
        result.append(line)
    return "\n".join(result)


def extract_facts_fallback(messages: list) -> list:
    facts = []
    patterns = [
        (r"I live in (.+)", "location", 1),
        (r"I work at (.+)", "job", 1),
        (r"I work as (.+)", "job", 1),
        (r"I'm a (.+)", "job", 1),
        (r"I moved to (.+)", "location", 1),
        (r"my name is (.+)", "name", 1),
        (r"I bought (.+)", "purchase", 1),
    ]

    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = msg.get("content", "")
        for pattern, ftype, conf in patterns:
            import re
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                facts.append(
                    {
                        "type": ftype,
                        "key": ftype,
                        "value": match.group(1).strip(),
                        "confidence": conf,
                    }
                )
    return facts[:3]
