import pg8000.native
import uuid
import json
import os
import time
from dataclasses import dataclass
from typing import List, Dict, Optional
from pydantic import BaseModel
from google import genai

# ==========================================
# 1. SETUP & MODELS
# ==========================================
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_NAME = os.getenv("DB_NAME")
DB_PASSWORD = os.getenv("DB_PASSWORD") # Set this in your environment!
DB_PORT = 5432
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

class QuestionAudit(BaseModel):
    question_id: str
    status: str       
    feedback: Optional[str] = None

class QuestionPair(BaseModel):
    primary_id: str
    backup_id: str

class AuditResponse(BaseModel):
    global_verdict: str
    global_summary: Optional[str] = None
    evaluations: List[QuestionAudit]
    detected_pairs: List[QuestionPair]

# ==========================================
# 2. DATABASE LOGIC
# ==========================================
def get_db():
    import ssl
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(
        user=DB_USER, host=DB_HOST, password=DB_PASSWORD, 
        database=DB_NAME, port=DB_PORT, ssl_context=ssl_ctx
    )

def fetch_next_unverified_group():
    """Fetches ONE unverified group at a time."""
    conn = get_db()
    
    group_query = """
        SELECT DISTINCT variant_group_id 
        FROM question_bank 
        WHERE verification_status != 'verified' OR verification_status IS NULL
        LIMIT 1
    """
    group_row = conn.run(group_query)
    
    if not group_row:
        conn.close()
        return None, [], None

    target_group_id = group_row[0][0]

    questions_query = """
        SELECT q.question_id, q.question_json, q.explanation, 
               q.variant_type, q.role, q.status, c.fact_text
        FROM question_bank q
        LEFT JOIN concept_cards c ON q.card_id = c.card_id
        WHERE q.variant_group_id = :gid 
        ORDER BY q.question_id ASC
    """
    rows = conn.run(questions_query, gid=target_group_id)
    questions = []
    shared_fact_text = rows[0][6] if rows else "No reference fact found."
    
    for row in rows:
        q_json = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        questions.append({
            "id": str(row[0]),
            "stem": q_json['stem'],
            "options": q_json['options'],
            "key": q_json['correct_key'],
            "expl": row[2],
            "role": row[4]
        })
        
    conn.close()
    return target_group_id, questions, shared_fact_text

def save_pairings_and_feedback(group_id, ai_response: AuditResponse):
    """Saves groupings, AI feedback, and marks the group as verified."""
    conn = get_db()
    try:
        # 1. Save Groupings
        for p in ai_response.detected_pairs:
            pair_uuid = str(uuid.uuid4())
            conn.run("""
                UPDATE question_bank SET question_group_id = :uid 
                WHERE question_id IN (:p_id, :b_id)
            """, uid=pair_uuid, p_id=p.primary_id, b_id=p.backup_id)

        # 2. Save AI Feedback to the database (Assuming you have an ai_feedback column)
        # If you don't have this column, add it: ALTER TABLE question_bank ADD COLUMN ai_feedback TEXT;
        for ev in ai_response.evaluations:
            if ev.status == "FAIL":
                conn.run("""
                    UPDATE question_bank SET ai_feedback = :fb 
                    WHERE question_id = :qid
                """, fb=ev.feedback, qid=ev.question_id)

        # 3. Mark group as verified so the script moves on to the next one
        conn.run("UPDATE question_bank SET verification_status = 'verified' WHERE variant_group_id = :gid", gid=group_id)
        
    finally:
        conn.close()

# ==========================================
# 3. THE AUTOMATION LOOP
# ==========================================
def run_automation():
    print("🚀 Starting FCPS Auto-Auditor...")
    groups_processed = 0

    while True:
        group_id, questions, shared_fact = fetch_next_unverified_group()
        
        if not group_id:
            print("✅ All groups in the database have been verified. Exiting.")
            break

        print(f"⏳ Processing Group: {group_id} ({len(questions)} questions)")

        # Build AI Prompt
        prompt = "Audit this medical question set (FCPS Part 1). Focus ONLY on factual accuracy.\n"
        prompt += f"Reference Fact (Context): {shared_fact}\n\n"
        for q in questions:
            opts_str = ", ".join([f"{o['key']}:{o['text']}" for o in q['options']])
            prompt += f"--- QUESTION ID {q['id']} ---\nRole: {q['role']}\nStem: {q['stem']}\nOptions: {opts_str}\nKey: {q['key']}\nExpl: {q['expl']}\n\n"
        
        prompt += """
        Tasks:
        1. VALIDATION: Check for factually incorrect statements, mismatches, or logic errors.
        2. PAIRING: Identify pairs of (Primary, Backup) questions that test the exact same concept.
           - A Primary can be paired with a Backup_Clone.
           - One Primary can only pair with one Backup_clone.
        3. Make sure you do not mix up question_id (uuid).

        OUTPUT JSON FORMAT:
        {
            "global_verdict": "PASS" or "FAIL",
            "global_summary": "Short note if FAIL, null if PASS",
            "evaluations":  [
                { "question_id": "uuid1", "status": "FAIL", "feedback": "Brief feedback." },
                { "question_id": "uuid2", "status": "PASS", "feedback": null }
            ],
            "detected_pairs": [
                { "primary_id": "uuid1", "backup_id": "uuid2" }
            ]
        }
        """
        
        try:
            # Call Gemini
            response = client.models.generate_content(
                model='gemma-3-27b-it', 
                contents=prompt,
                config={'response_mime_type': 'application/json', 'response_schema': AuditResponse}
            )
            res = response.parsed
            
            # Save results to DB
            save_pairings_and_feedback(group_id, res)
            
            groups_processed += 1
            print(f"   --> Success! {len(res.detected_pairs)} pairs grouped. Verdict: {res.global_verdict}")
            
            # Sleep to respect Gemini API rate limits (adjust as needed)
            time.sleep(2) 

        except Exception as e:
            print(f"❌ Error processing group {group_id}: {e}")
            break # Stop the script on severe error to prevent loops

    print(f"🏁 Automation complete. Processed {groups_processed} groups.")

if __name__ == "__main__":
    run_automation()
