import streamlit as st
import pg8000.native
import ssl
import json
import os
from dataclasses import dataclass
from typing import List, Dict, Optional
from pydantic import BaseModel
from google import genai

# ==============================================================================
# 1. CONFIGURATION & STYLING
# ==============================================================================
st.set_page_config(page_title="FCPS Auditor", layout="centered", page_icon="🩺")

st.markdown("""
<style>
    div.stButton > button {
        width: 100%;
        border-radius: 8px;
        height: 40px;
        font-weight: 500;
        border: 1px solid #ddd;
    }
    div.stButton > button[kind="primary"] {
        background-color: #0f9d58; 
        border-color: #0f9d58;
    }
    .element-container { margin-bottom: 1rem; }
    
    .explanation-text {
        color: #fff; 
        font-size: 0.95em; 
        line-height: 1.5;
    }
</style>
""", unsafe_allow_html=True)

DB_HOST = os.getenv("DB_HOST", "psql-qbank-core-01.postgres.database.azure.com")
DB_USER = os.getenv("DB_USER", "rmhadmin")
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_PORT = 5432

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ==============================================================================
# 2. SESSION & MODELS
# ==============================================================================
if "skipped_groups" not in st.session_state:
    st.session_state["skipped_groups"] = []

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

class QuestionAudit(BaseModel):
    question_id: int
    status: str       # "PASS" or "FAIL"
    feedback: Optional[str] = None

class AuditResponse(BaseModel):
    global_verdict: str
    global_summary: Optional[str] = None
    evaluations: List[QuestionAudit]

@dataclass
class QuestionData:
    question_id: int
    stem: str
    options: List[Dict]
    correct_key: str
    explanation: str
    variant_type: str
    role: str
    status: str             # 'active' or 'inactive'
    verification_status: str # 'pending' or 'verified'

# ==============================================================================
# 3. DATABASE LOGIC
# ==============================================================================
def get_db():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(
        user=DB_USER, host=DB_HOST, password=st.session_state["db_password"], 
        database=DB_NAME, port=DB_PORT, ssl_context=ssl_ctx
    )

def try_connect(password):
    try:
        st.session_state["db_password"] = password
        conn = get_db()
        conn.close()
        return True, None
    except Exception as e:
        return False, str(e)

def fetch_variant_group(skipped_ids):
    conn = get_db()
    
    # Logic: Find a group where verification_status IS NOT 'verified'
    
    if not skipped_ids:
        group_query = """
            SELECT DISTINCT variant_group_id 
            FROM question_bank 
            WHERE verification_status != 'verified' OR verification_status IS NULL
            LIMIT 1
        """
        group_row = conn.run(group_query)
    else:
        group_query = """
            SELECT DISTINCT variant_group_id 
            FROM question_bank 
            WHERE (verification_status != 'verified' OR verification_status IS NULL)
            AND variant_group_id != ALL(:skip_list)
            LIMIT 1
        """
        group_row = conn.run(group_query, skip_list=skipped_ids)
    
    if not group_row:
        conn.close()
        return None, []

    target_group_id = group_row[0][0]

    # Fetch columns including the new verification_status
    questions_query = """
        SELECT question_id, question_json, explanation, variant_type, role, status, verification_status
        FROM question_bank WHERE variant_group_id = :gid ORDER BY question_id ASC
    """
    rows = conn.run(questions_query, gid=target_group_id)
    questions = []
    for row in rows:
        qid, q_json_str, q_expl, q_var, q_role, q_stat, q_verif = row
        q_json = json.loads(q_json_str) if isinstance(q_json_str, str) else q_json_str
        
        # Handle NULL verification status gracefully
        verif_val = q_verif if q_verif else 'pending'
        
        questions.append(QuestionData(
            question_id=qid, stem=q_json['stem'], options=q_json['options'],
            correct_key=q_json['correct_key'], explanation=q_expl, variant_type=q_var,
            role=q_role, status=q_stat, verification_status=verif_val
        ))
    conn.close()
    return target_group_id, questions

def save_edit(qid, new_json, new_expl):
    conn = get_db()
    conn.run("UPDATE question_bank SET question_json = :qj, explanation = :ex WHERE question_id = :qid", 
             qj=json.dumps(new_json), ex=new_expl, qid=qid)
    conn.close()

def update_status_single(qid, new_status):
    # Updates Active/Inactive ONLY
    conn = get_db()
    conn.run("UPDATE question_bank SET status = :s WHERE question_id = :qid", s=new_status, qid=qid)
    conn.close()

def mark_group_verified(group_id):
    # Updates Verification Status ONLY
    conn = get_db()
    conn.run("UPDATE question_bank SET verification_status = 'verified' WHERE variant_group_id = :gid", gid=group_id)
    conn.close()

# ==============================================================================
# 4. LOGIN SCREEN
# ==============================================================================
if not st.session_state["authenticated"]:
    st.markdown("### 🔐 Login")
    pwd = st.text_input("Database Password", type="password")
    if st.button("Connect"):
        valid, err = try_connect(pwd)
        if valid:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error(err)
    st.stop()

# ==============================================================================
# 5. MAIN UI
# ==============================================================================
if 'group_data' not in st.session_state:
    st.session_state.group_data = fetch_variant_group(st.session_state["skipped_groups"])

group_id, questions = st.session_state.group_data

# Global Status Placeholder
global_verdict_container = st.empty()
global_verdict_container.info("⏳ Auditing...")

if not group_id:
    st.balloons()
    st.success("All questions verified!")
    if st.button("Reset Session"):
        st.session_state["skipped_groups"] = []
        st.cache_data.clear()
        st.session_state.group_data = fetch_variant_group([])
        st.rerun()
    st.stop()

question_feedback_map = {}

# --- RENDER QUESTIONS ---
for q in questions:
    
    with st.container(border=True):
        # Header: Role | Active Status | Verification Status
        c1, c2, c3 = st.columns([0.6, 0.2, 0.2])
        with c1:
            icon = "🔹" if q.role == "Primary" else "🔗"
            st.markdown(f"**{icon} {q.role}**")
            st.caption(f"{q.variant_type}")
        with c2:
            # Active/Inactive
            if q.status == 'active':
                st.markdown('<span style="color:green; font-weight:bold;">ACTIVE</span>', unsafe_allow_html=True)
            else:
                st.markdown('<span style="color:red; font-weight:bold;">INACTIVE</span>', unsafe_allow_html=True)
        with c3:
            # Verification Badge
            if q.verification_status == 'verified':
                st.markdown("✅ Verif")
            else:
                st.markdown("⚠️ Pend")

        question_feedback_map[q.question_id] = st.empty()

        edit_key = f"edit_{q.question_id}"
        
        if st.session_state.get(edit_key, False):
            # === EDIT MODE ===
            new_stem = st.text_area("Vignette", q.stem, height=100)
            new_key = st.selectbox("Correct Option", ["A","B","C","D","E"], 
                                   index=["A","B","C","D","E"].index(q.correct_key),
                                   key=f"k_{q.question_id}")
            new_expl = st.text_area("Explanation", q.explanation)
            
            b1, b2 = st.columns(2)
            if b1.button("💾 Save", key=f"sv_{q.question_id}", type="primary"):
                new_json = {"stem": new_stem, "options": q.options, "correct_key": new_key}
                save_edit(q.question_id, new_json, new_expl)
                st.session_state[edit_key] = False
                st.rerun()
            if b2.button("Cancel", key=f"cn_{q.question_id}"):
                st.session_state[edit_key] = False
                st.rerun()
        else:
            # === READ MODE ===
            st.write(q.stem)
            
            for opt in q.options:
                if opt['key'] == q.correct_key:
                    st.markdown(f":green[**{opt['key']}) {opt['text']}**]")
                else:
                    st.markdown(f"{opt['key']}) {opt['text']}")
            
            st.markdown("---")
            st.markdown("**Explanation**")
            st.markdown(f"<div class='explanation-text'>{q.explanation}</div>", unsafe_allow_html=True)

            st.markdown("---")
            f1, f2 = st.columns(2)
            if f1.button("✏️ Edit", key=f"ed_{q.question_id}"):
                st.session_state[edit_key] = True
                st.rerun()
            
            tog_text = "Deactivate 🚫" if q.status == 'active' else "Activate ✅"
            if f2.button(tog_text, key=f"tg_{q.question_id}"):
                new_s = 'inactive' if q.status == 'active' else 'active'
                update_status_single(q.question_id, new_s)
                st.rerun()

# --- BOTTOM BAR ---
st.divider()
bc1, bc2 = st.columns(2)

if bc1.button("⏭️ Skip Group"):
    st.session_state["skipped_groups"].append(group_id)
    st.session_state.group_data = fetch_variant_group(st.session_state["skipped_groups"])
    st.rerun()

if bc2.button("✅ Verify All", type="primary"):
    mark_group_verified(group_id)
    st.toast("Verified!")
    st.session_state.group_data = fetch_variant_group(st.session_state["skipped_groups"])
    st.rerun()

# ==============================================================================
# 6. AI INJECTION (YOUR CUSTOM PROMPT)
# ==============================================================================
prompt = "Audit this medical question set (FCPS Part 1). Focus ONLY on factual accuracy.\n\n"

for i, q in enumerate(questions):
    opts_str = ", ".join([f"{o['key']}:{o['text']}" for o in q.options])
    prompt += f"--- QUESTION ID {q.question_id} ---\n"
    prompt += f"Stem: {q.stem}\nOptions: {opts_str}\nKey: {q.correct_key}\nExpl: {q.explanation}\n\n"

prompt += """
Check for: 
1. Factually incorrect medical statements.
2. Wrong Answer Key (e.g. Explanation says A but Key says B).
3. Two correct options.
4. Any other inaccuracies.

Task:
1. If ALL questions are correct, global_verdict = "PASS".
2. If ANY question has a factual error, global_verdict = "FAIL".
3. Only provide feedback for specific questions that have errors.

OUTPUT JSON FORMAT:
{
    "global_verdict": "PASS" or "FAIL",
    "global_summary": "Short note if FAIL, null if PASS",
    "evaluations": [
        { "question_id": 123, "status": "FAIL", "feedback": "Wrong dose." },
        { "question_id": 456, "status": "PASS", "feedback": null }
    ]
}
"""

try:
    response = client.models.generate_content(
        model='gemini-3-flash-preview',
        contents=prompt,
        config={
            'response_mime_type': 'application/json',
            'response_schema': AuditResponse,
        }
    )
    res: AuditResponse = response.parsed
    
    if res.global_verdict == "PASS":
        global_verdict_container.success("✅ **AI VERDICT: PASS**")
    else:
        global_verdict_container.error(f"❌ **AI VERDICT: FAIL** - {res.global_summary}")

    for ev in res.evaluations:
        if ev.status == "FAIL" and ev.question_id in question_feedback_map:
            question_feedback_map[ev.question_id].error(f"**AI Error Detected:** {ev.feedback}")

except Exception as e:
    global_verdict_container.warning("⚠️ AI Audit Connecting...")
