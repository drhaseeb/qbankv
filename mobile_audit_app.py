import streamlit as st
import pg8000.native
import ssl
import os
from dataclasses import dataclass
from typing import List, Dict, Optional
from pydantic import BaseModel
from google import genai # <--- NEW SDK

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
DB_HOST = os.getenv("DB_HOST", "psql-qbank-core-01.postgres.database.azure.com")
DB_USER = os.getenv("DB_USER", "rmhadmin")
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_PORT = 5432

# Initialize New Client
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ==============================================================================
# 2. PYDANTIC MODELS (For Structured Output)
# ==============================================================================
class QuestionAudit(BaseModel):
    index: int
    status: str  # "PASS" or "FAIL"
    feedback: Optional[str] = None

class AuditResponse(BaseModel):
    evaluations: List[QuestionAudit]

# ==============================================================================
# 3. DATABASE LOGIC
# ==============================================================================
def get_db():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    # Use session password
    return pg8000.native.Connection(
        user=DB_USER, host=DB_HOST, password=st.session_state["db_password"], 
        database=DB_NAME, port=DB_PORT, ssl_context=ssl_ctx
    )

def try_connect(password):
    try:
        st.session_state["db_password"] = password # Temp store to test
        conn = get_db()
        conn.close()
        return True, None
    except Exception as e:
        return False, str(e)

@dataclass
class QuestionData:
    question_id: int
    stem: str
    options: List[Dict]
    correct_key: str
    explanation: str
    variant_type: str
    role: str
    status: str

def fetch_variant_group():
    conn = get_db()
    # Find unverified group
    group_query = "SELECT DISTINCT variant_group_id FROM question_bank WHERE status != 'verified' LIMIT 1"
    group_row = conn.run(group_query)
    
    if not group_row:
        conn.close()
        return None, []

    target_group_id = group_row[0][0]

    # Fetch questions
    questions_query = """
        SELECT question_id, question_json, explanation, variant_type, role, status
        FROM question_bank WHERE variant_group_id = :gid ORDER BY question_id ASC
    """
    rows = conn.run(questions_query, gid=target_group_id)
    questions = []
    for row in rows:
        qid, q_json_str, q_expl, q_var, q_role, q_stat = row
        q_json = json.loads(q_json_str) if isinstance(q_json_str, str) else q_json_str
        questions.append(QuestionData(
            question_id=qid, stem=q_json['stem'], options=q_json['options'],
            correct_key=q_json['correct_key'], explanation=q_expl, variant_type=q_var,
            role=q_role, status=q_stat
        ))
    conn.close()
    return target_group_id, questions

def save_edit(qid, new_json, new_expl):
    conn = get_db()
    conn.run("UPDATE question_bank SET question_json = :qj, explanation = :ex WHERE question_id = :qid", 
             qj=json.dumps(new_json), ex=new_expl, qid=qid)
    conn.close()

def update_status_single(qid, new_status):
    conn = get_db()
    conn.run("UPDATE question_bank SET status = :s WHERE question_id = :qid", s=new_status, qid=qid)
    conn.close()

def mark_group_verified(group_id):
    conn = get_db()
    conn.run("UPDATE question_bank SET status = 'verified' WHERE variant_group_id = :gid", gid=group_id)
    conn.close()

import json # Need standard json for DB writes

# ==============================================================================
# 4. APP START & LOGIN
# ==============================================================================
st.set_page_config(page_title="FCPS Live Audit", layout="centered")

if not st.session_state.get("authenticated", False):
    st.markdown("## 🔐 Database Login")
    pwd = st.text_input("Database Password", type="password")
    if st.button("Login"):
        valid, err = try_connect(pwd)
        if valid:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error(err)
    st.stop()

# ==============================================================================
# 5. UI & INJECTION LOGIC
# ==============================================================================
if 'group_data' not in st.session_state:
    st.session_state.group_data = fetch_variant_group()
    # We do NOT store audit_result in session state anymore because 
    # we want to re-run it fresh if data changes, but we check cache inside function

group_id, questions = st.session_state.group_data

# Toolbar
with st.container():
    c1, c2 = st.columns([0.8, 0.2])
    c1.progress(100, text=f"Group: {group_id}")
    if c2.button("🔄"):
        st.cache_data.clear()
        st.session_state.group_data = fetch_variant_group()
        st.rerun()

if not group_id:
    st.success("🎉 All caught up!")
    st.stop()

# --- DICTIONARY TO HOLD PLACEHOLDERS ---
# This is the magic. We create empty slots now, and fill them later.
audit_placeholders = {} 

for i, q in enumerate(questions):
    
    with st.container(border=True):
        # Header Line
        h1, h2, h3 = st.columns([0.6, 0.2, 0.2])
        h1.markdown(f"**{q.role}**: {q.variant_type}")
        
        stat_color = "green" if q.status == 'active' else "red"
        h2.markdown(f":{stat_color}[{q.status}]")
        
        # --- CREATE PLACEHOLDER FOR AI ICON ---
        # We save this specific object to write to it later
        audit_placeholders[i+1] = h3.empty()
        audit_placeholders[i+1].write("⏳") 

        # --- CREATE PLACEHOLDER FOR FEEDBACK TEXT ---
        # Only used if AI fails
        feedback_placeholder = st.empty()
        audit_placeholders[f"fb_{i+1}"] = feedback_placeholder

        # --- READ/EDIT UI ---
        edit_key = f"edit_mode_{q.question_id}"
        if st.session_state.get(edit_key, False):
            new_stem = st.text_area("Stem", q.stem, key=f"s_{q.question_id}")
            new_expl = st.text_area("Explanation", q.explanation, key=f"e_{q.question_id}")
            new_key = st.selectbox("Key", ["A","B","C","D","E"], index=["A","B","C","D","E"].index(q.correct_key), key=f"k_{q.question_id}")
            
            if st.button("💾 Save", key=f"sv_{q.question_id}"):
                new_json = {"stem": new_stem, "options": q.options, "correct_key": new_key}
                save_edit(q.question_id, new_json, new_expl)
                st.session_state[edit_key] = False
                st.rerun()
        else:
            st.write(q.stem)
            st.markdown("---")
            for opt in q.options:
                if opt['key'] == q.correct_key:
                    st.markdown(f":green[**{opt['key']}) {opt['text']}**]  *(Correct)*")
                else:
                    st.markdown(f"{opt['key']}) {opt['text']}")
            st.markdown("---")
            with st.expander("Explanation"):
                st.info(q.explanation)
            
            # Action Buttons
            b1, b2 = st.columns(2)
            if b1.button("✏️ Edit", key=f"ed_{q.question_id}"):
                st.session_state[edit_key] = True
                st.rerun()
            
            t_label = "Disable 🚫" if q.status == 'active' else "Enable ✅"
            t_val = 'inactive' if q.status == 'active' else 'active'
            if b2.button(t_label, key=f"tg_{q.question_id}"):
                update_status_single(q.question_id, t_val)
                st.rerun()

# Footer
st.divider()
fc1, fc2 = st.columns(2)
if fc1.button("⏭️ Skip"):
    st.session_state.group_data = fetch_variant_group()
    st.rerun()
if fc2.button("✅ Verify All", type="primary"):
    mark_group_verified(group_id)
    st.toast("Saved!")
    st.session_state.group_data = fetch_variant_group()
    st.rerun()

# ==============================================================================
# 6. INJECTION ENGINE (Runs at the end)
# ==============================================================================
# This runs AFTER the UI is drawn. It will "fill in" the placeholders.

prompt = "Audit these medical questions (FCPS Part 1). Focus on factual accuracy of the Stem, the Options, the Key and the Explanation.\n\n"

for i, q in enumerate(questions):
    # 1. Format the options into a clean string
    # e.g., "A) Aspirin\nB) Paracetamol..."
    opts_str = "\n".join([f"{opt['key']}) {opt['text']}" for opt in q.options])
    
    # 2. Build the readable block
    prompt += f"--- QUESTION {i+1} ({q.role}: {q.variant_type}) ---\n"
    prompt += f"Stem: {q.stem}\n"
    prompt += f"Options:\n{opts_str}\n"        # <--- Clean options list
    prompt += f"Correct Answer: {q.correct_key}\n" # <--- Clear label
    prompt += f"Explanation: {q.explanation}\n\n"

# Using the new Google GenAI SDK with Structured Output
try:
    response = client.models.generate_content(
        model='gemini-3-flash-preview',
        contents=prompt,
        config={
            'response_mime_type': 'application/json',
            'response_schema': AuditResponse, # Pydantic Model!
        }
    )
    
    # Parse the Pydantic object directly
    result: AuditResponse = response.parsed
    
    # Inject Results!
    for evaluation in result.evaluations:
        idx = evaluation.index
        
        # 1. Update Icon
        if evaluation.status == "PASS":
            audit_placeholders[idx].write("✅")
        else:
            audit_placeholders[idx].write("❌")
            
        # 2. Update Feedback (Only if Fail)
        if evaluation.status == "FAIL" and evaluation.feedback:
            audit_placeholders[f"fb_{idx}"].error(f"AI: {evaluation.feedback}")

except Exception as e:
    # If AI fails, turn all hourglasses to warning signs
    for i in range(len(questions)):
        audit_placeholders[i+1].write("⚠️")
        audit_placeholders[f"fb_{i+1}"].caption(f"AI Error: {str(e)}")
