#!/usr/bin/env python3
"""
Process downloaded LongMemEval JSON file to our fixture format.
"""

import json
import os
import requests

# --- Script Configuration ---
DATA_DIR = "data"
RAW_DATA_FILE = os.path.join(DATA_DIR, "longmemeval_s_cleaned_raw.json")
DATASET_URL = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"
# ---

# Ensure the data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# 1. Download the dataset if it doesn't exist
if not os.path.exists(RAW_DATA_FILE):
    print(f"Downloading dataset from {DATASET_URL}...")
    try:
        response = requests.get(DATASET_URL, stream=True, timeout=120)
        response.raise_for_status()
        with open(RAW_DATA_FILE, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Dataset downloaded successfully to {RAW_DATA_FILE}")
    except requests.exceptions.RequestException as e:
        print(f"Error: Failed to download dataset. {e}")
        print("Please download it manually and place it in the 'data/' directory.")
        exit(1)
else:
    print(f"Dataset already exists at {RAW_DATA_FILE}. Skipping download.")

# Map our categories to LongMemEval question_types
CATEGORY_MAP = {
    "single-session-user": "fact_extraction",
    "single-session-assistant": "fact_extraction", 
    "single-session-preference": "preferences_opinions",
    "temporal-reasoning": "fact_extraction",
    "knowledge-update": "fact_evolution",
    "multi-session": "multi_hop",
}

def convert_entry(entry):
    """Convert LongMemEval entry to our fixture format."""
    question_id = entry.get("question_id", "unknown")
    haystack_sessions = entry.get("haystack_sessions", [])
    
    turns = []
    for session_idx, session in enumerate(haystack_sessions):
        session_id = entry.get("haystack_session_ids", [f"session-{session_idx}"])[session_idx] if session_idx < len(entry.get("haystack_session_ids", [])) else f"session-{session_idx}"
        
        # The dataset has some inconsistencies in its structure, this handles them.
        actual_session = session[0] if isinstance(session, list) and len(session) > 0 and isinstance(session[0], list) else session

        for turn in actual_session:
            # The 'messages' field in our contract expects a list of turns.
            # Here, each 'turn' from the dataset becomes one item in that list.
            turns.append({
                "session_id": session_id,
                "user_id": question_id,
                "messages": [turn],
                "timestamp": entry.get("haystack_dates", ["2025-01-01T00:00:00Z"])[session_idx] if session_idx < len(entry.get("haystack_dates", [])) else "2025-01-01T00:00:00Z",
                "metadata": {"session_index": session_idx}
            })
    
    return {
        "question_id": question_id,
        "category": CATEGORY_MAP.get(entry.get("question_type", ""), "fact_extraction"),
        "question_type": entry.get("question_type", ""),
        "turns": turns,
        "question": entry.get("question", ""),
        "expected_answer": entry.get("answer", ""),
        "answer_session_ids": entry.get("answer_session_ids", []),
        "haystack_session_ids": entry.get("haystack_session_ids", []),
    }


# Load the downloaded file
print(f"Loading raw data from {RAW_DATA_FILE}...")
with open(RAW_DATA_FILE, "r") as f:
    data = json.load(f)


print(f"Loaded {len(data)} entries")

# Convert all entries
print("Converting entries...")
tests = []

for i, entry in enumerate(data):
    try:
        converted = convert_entry(entry)
        tests.append(converted)
        
        if (i + 1) % 50 == 0:
            print(f"Converted {i + 1}/{len(data)}...")
            
    except Exception as e:
        print(f"Error converting entry {i}: {e}")

print(f"\nTotal converted: {len(tests)}")

# Category breakdown
category_counts = {}
for t in tests:
    cat = t.get("category", "unknown")
    category_counts[cat] = category_counts.get(cat, 0) + 1

print("Category breakdown:")
for cat, count in sorted(category_counts.items()):
    print(f"  {cat}: {count}")

# Save full dataset
output = {
    "source": "LongMemEval (xiaowu0162/longmemeval-cleaned)",
    "split": "longmemeval_s_cleaned",
    "total_tests": len(tests),
    "categories": category_counts,
    "tests": tests
}

output_path = os.path.join(DATA_DIR, "longmemeval_s_cleaned.json")
os.makedirs(os.path.dirname(output_path), exist_ok=True)
with open(output_path, "w") as f:
    json.dump(output, f, indent=2)

print(f"\nSaved {len(tests)} tests to {output_path}")

# Create smaller test sets
for limit in [100, 50]:
    small_output = {
        "source": "LongMemEval (xiaowu0162/longmemeval-cleaned)",
        "split": "longmemeval_s_cleaned",
        "total_tests": limit,
        "categories": category_counts,
        "tests": tests[:limit]
    }
    
    small_path = os.path.join(DATA_DIR, f"longmemeval_{limit}.json")
    with open(small_path, "w") as f:
        json.dump(small_output, f, indent=2)
    
    print(f"Saved {limit} tests to {small_path}")

print("\nDone!")