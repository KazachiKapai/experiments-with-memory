import os
import json
import logging
from typing import List, Dict, Any, Optional

from src.errors import APIError
from src.database import conn

logger = logging.getLogger(__name__)

# --- Azure OpenAI Setup ---
AZURE_CONFIG = {
    "endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
    "api_key": os.environ.get("AZURE_OPENAI_KEY", ""),
    "deployment": os.environ.get("AZURE_DEPLOYMENT_NAME", "gpt-oss-120b"),
}

_azure_client = None


def get_azure_client():
    global _azure_client
    if not _azure_client and AZURE_CONFIG["endpoint"] and AZURE_CONFIG["api_key"]:
        try:
            from openai import AzureOpenAI
            _azure_client = AzureOpenAI(
                azure_endpoint=AZURE_CONFIG["endpoint"],
                api_key=AZURE_CONFIG["api_key"],
                api_version="2024-02-01",
            )
        except ImportError:
            logger.warning("openai package not installed")
        except Exception as e:
            logger.warning(f"Failed to initialize Azure client: {e}")
    return _azure_client


def get_embedding(text: str) -> List[float]:
    try:
        client = get_azure_client()
        if not client:
            raise APIError("Azure client not available", status_code=503)

        response = client.embeddings.create(model="text-embedding-3-large", input=text)
        return response.data[0].embedding
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise APIError("Failed to generate embedding", status_code=503, details={"error": str(e)})


async def extract_facts_from_history(history: List[Dict[str, Any]], user_id: str, turn_id: str) -> List[Dict[str, Any]]:
    client = get_azure_client()
    if not client or not history:
        return []

    user_messages = [
        m.get("content", "")
        for turn_data in history
        for m in turn_data.get("messages", [])
        if m.get("role") == "user" and m.get("content")
    ]
    user_text = "\n".join([f"[USER]: {msg}" for msg in user_messages])[-15000:]

    try:
        # Pass 1
        response = client.chat.completions.create(
            model=AZURE_CONFIG["deployment"],
            messages=[
                {"role": "system", "content": "Extract ALL facts. Be exhaustive. Return ONLY valid JSON array."},
                {
                    "role": "user",
                    "content": f"""Extract ONLY facts that USER explicitly mentioned about themselves.
Look for: names, personal details, times (XX minutes, XX hours), dates, numbers, prices, quantities, counts, locations, events, achievements, possessions, preferences, family, work, education, hobbies, health issues.

IMPORTANT: Only extract from USER messages. Ignore assistant responses.

Return ALL facts as JSON array.
Format: [{{"type": "fact", "key": "descriptive_key", "value": "exact fact", "confidence": 0.9}}]

USER MESSAGES:
{user_text}

Extract all facts (0-20):""",
                },
            ],
            temperature=0.1,
            max_tokens=3000,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        facts = json.loads(content.strip())
        if not isinstance(facts, list):
            facts = []

        # Pass 2
        response2 = client.chat.completions.create(
            model=AZURE_CONFIG["deployment"],
            messages=[
                {"role": "system", "content": "Find specific details user mentioned."},
                {
                    "role": "user",
                    "content": f"""Search for any facts the user mentioned that might have been missed.
Look for: exact times (XX minutes XX seconds), dates, numbers, prices, quantities, counts, specific achievements, personal bests, family details, work history.
Only look in USER messages.

USER MESSAGES:
{user_text}

Return any missed facts as JSON array:""",
                },
            ],
            temperature=0.1,
            max_tokens=1500,
        )
        content2 = response2.choices[0].message.content.strip()
        if content2.startswith("```"):
            content2 = content2.split("```")[1]
            if content2.startswith("json"):
                content2 = content2[4:]
        more_facts = json.loads(content2.strip())
        if isinstance(more_facts, list):
            facts.extend(more_facts)

        return facts
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Fact extraction error: {e}")
        return []


def get_user_total_tokens(user_id: str) -> int:
    cur = conn.execute("SELECT turn_json FROM user_conversations WHERE user_id=?", (user_id,))
    total = 0
    for row in cur.fetchall():
        try:
            data = json.loads(row["turn_json"])
            messages = data.get("messages", [])
            for m in messages:
                total += count_tokens(m.get("content", ""))
        except:
            pass
    return total


async def extract_implicit_facts(user_id: str, session_id: str) -> list:
    client = get_azure_client()
    if not client:
        return []

    total_tokens = get_user_total_tokens(user_id)
    if total_tokens < 60000:
        return []

    cur = conn.execute("SELECT turn_json FROM user_conversations WHERE user_id=?", (user_id,))
    all_messages = [
        m
        for row in cur.fetchall()
        for m in json.loads(row["turn_json"]).get("messages", [])
    ]

    user_text = "\n".join(
        [f"[{m.get('role')}]: {m.get('content', '')}" for m in all_messages if m.get("content")]
    )[-50000:]

    try:
        response = client.chat.completions.create(
            model=AZURE_CONFIG["deployment"],
            messages=[
                {"role": "system", "content": "You're an agent who uncovers hidden facts. Analyze patterns and behavior."},
                {
                    "role": "user",
                    "content": f"""You are an agent tasked with uncovering implicit facts. Analyze patterns and behavior. Examine the entire history and identify IMPLICIT facts and behavioral patterns.
                    Analyze the entire history and identify IMPLICIT facts and behavioral patterns. Implicit facts include:
                    - Recurring topics (asks about React every week)
                    - Patterns (back hurts several times → chronic pain)
                    - Preferences (always chooses certain restaurants)
                    - Behavioral patterns (gets tired after work, looks for news in the evenings)

                    Return a JSON array with the implicit facts:
                    [{{"type": "pattern", "key": "description", "value": "specific pattern", "confidence": 0.6}}]
                    History ({total_tokens} tokens):
                    {user_text}

                    Extract implicit facts (0–10):""",
                },
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        
        facts = json.loads(content.strip())
        return facts if isinstance(facts, list) else []
    except Exception as e:
        logger.error(f"Implicit agent error: {e}")
        return []

_token_encoder = None

def get_token_encoder():
    global _token_encoder
    if _token_encoder is None:
        try:
            import tiktoken
            _token_encoder = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            logger.warning("tiktoken not installed, using fallback token count")
            _token_encoder = False
        except Exception as e:
            logger.warning(f"Failed to load tiktoken: {e}")
            _token_encoder = False
    return _token_encoder if _token_encoder else None


def count_tokens(text: str) -> int:
    enc = get_token_encoder()
    if enc:
        return len(enc.encode(text))
    return int(len(text) * 0.3)
