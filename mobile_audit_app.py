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

# CUSTOM CSS: Fixes button alignment, colors, and spacing
st.markdown("""
<style>
    /* Make buttons fill their columns and align perfectly */
    div.stButton > button {
        width: 100%;
        border-radius: 8px;
        height: 38px;
        border: 1px solid #e0e0e0;
        font-weight: 500;
    }
    
    /* Primary Action Button (Verify) */
    div.stButton > button[kind="primary"] {
        background-color: #0f9d58; 
        border-color: #0f9d58;
    }

    /* Card-like look for expanders */
    .streamlit-expanderHeader {
        background-color: #f8f9fa;
        border-radius: 8px;
    }
    
    /* Clean status badges */
    .status-badge {
        padding: 4px 8px;
        border-radius: 4px;
        font-size: 0.8em;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)

DB_HOST = os.getenv("DB_HOST", "psql-qbank-core-01.postgres.database.azure.com")
DB_USER = os.getenv("DB_USER", "rmhadmin")
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_PORT = 5432

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ==============================================================================
# 2. DATA MODELS
# ==============================================================================
class QuestionAudit(BaseModel):
    question_id: int  # Mapping back by ID is safer than Index
    status: str       # "PASS" or "FAIL"
    feedback: Optional[str] = None

class AuditResponse(BaseModel):
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
    status: str

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

def fetch_variant_group():
    conn = get_db()
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

# ==============================================================================
# 4. LOGIN SCREEN
# ==============================================================================
if not st.session_state.get("authenticated", False):
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
# 5. MAIN UI & PLACEHOLDER LOGIC
# ==============================================================================
if 'group_data' not in st.session_state:
    st.session_state.group_data = fetch_variant_group()

group_id, questions = st.session_state.group_data

# Top Bar
c1, c2 = st.columns([0.85, 0.15])
with c1:
    st.caption(f"Reviewing Variant Group: {group_id}")
    st.progress(100)
with c2:
    if st.button("🔄", help="Refresh Data"):
        st.cache_data.clear()
        st.session_state.group_data = fetch_variant_group()
        st.rerun()

if not group_id:
    st.balloons()
    st.success("You are all caught up! No unverified questions found.")
    st.stop()

# --- PLACEHOLDER REGISTRY ---
# We store widgets here to update them later with AI results
ai_status_icons = {}
ai_feedback_boxes = {}

# --- RENDER QUESTIONS LOOP ---
for q in questions:
    
    # CARD CONTAINER
    with st.container(border=True):
        
        # 1. HEADER ROW (Role | Status | AI Spinner)
        h1, h2, h3 = st.columns([0.6, 0.25, 0.15])
        
        with h1:
            # Clean Role Badge
            icon = "🔹" if q.role == "Primary" else "🔗"
            st.markdown(f"**{icon} {q.role}**")
            st.caption(f"{q.variant_type}")

        with h2:
            # Status Badge
            if q.status == 'active':
                st.markdown('<span style="color:green; background:#e6f4ea; padding:2px 6px; border-radius:4px;">ACTIVE</span>', unsafe_allow_html=True)
            else:
                st.markdown('<span style="color:red; background:#fce8e6; padding:2px 6px; border-radius:4px;">INACTIVE</span>', unsafe_allow_html=True)

        with h3:
            # AI Placeholders
            ai_status_icons[q.question_id] = st.empty()
            ai_status_icons[q.question_id].write("⏳") # Loading state

        # 2. FEEDBACK ROW (Hidden unless fail)
        ai_feedback_boxes[q.question_id] = st.empty()

        # 3. CONTENT AREA
        edit_key = f"edit_mode_{q.question_id}"
        
        if st.session_state.get(edit_key, False):
            # === EDIT MODE ===
            new_stem = st.text_area("Vignette", q.stem, height=100)
            new_key = st.selectbox("Correct Option", ["A","B","C","D","E"], 
                                   index=["A","B","C","D","E"].index(q.correct_key),
                                   key=f"k_{q.question_id}")
            new_expl = st.text_area("Explanation", q.explanation)
            
            b1, b2 = st.columns(2)
            if b1.button("💾 Save Changes", key=f"sv_{q.question_id}", type="primary"):
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
            
            # Options Display (Compact)
            opt_md = ""
            for opt in q.options:
                key = opt['key']
                text = opt['text']
                if key == q.correct_key:
                    opt_md += f"- :green[**{key}) {text}**] (Key)\n"
                else:
                    opt_md += f"- {key}) {text}\n"
            st.markdown(opt_md)
            
            # Explanation (Collapsible)
            with st.expander("Show Explanation"):
                st.info(q.explanation)

            # Footer Actions
            f1, f2 = st.columns(2)
            if f1.button("✏️ Edit", key=f"ed_{q.question_id}"):
                st.session_state[edit_key] = True
                st.rerun()
            
            tog_label = "Deactivate 🚫" if q.status == 'active' else "Activate ✅"
            if f2.button(tog_label, key=f"tg_{q.question_id}"):
                new_s = 'inactive' if q.status == 'active' else 'active'
                update_status_single(q.question_id, new_s)
                st.rerun()

# --- BOTTOM ACTION BAR ---
st.divider()
ac1, ac2 = st.columns(2)

if ac1.button("⏭️ Skip Group"):
    st.session_state.group_data = fetch_variant_group()
    st.rerun()

if ac2.button("✅ Verify All", type="primary"):
    mark_group_verified(group_id)
    st.toast("Marked as Verified!")
    st.session_state.group_data = fetch_variant_group()
    st.rerun()

# ==============================================================================
# 6. AI INJECTION (Runs automatically at end of script)
# ==============================================================================
prompt = "Audit these medical questions (FCPS Part 1). Focus ONLY on factual errors.\n\n"

for i, q in enumerate(questions):
    # Format options cleanly for AI
    opts_str = ", ".join([f"{o['key']}:{o['text']}" for o in q.options])
    
    prompt += f"--- QUESTION ID {q.question_id} ---\n"
    prompt += f"Stem: {q.stem}\n"
    prompt += f"Options: {opts_str}\n"
    prompt += f"Correct Key: {q.correct_key}\n"
    prompt += f"Explanation: {q.explanation}\n\n"

prompt += """
Check for: 
1. Factually incorrect medical statements.
2. Wrong Answer Key (e.g. Explanation says A but Key says B).
3. Two correct options.

OUTPUT JSON FORMAT:
{
    "evaluations": [
        { "question_id": 123, "status": "PASS", "feedback": null },
        { "question_id": 456, "status": "FAIL", "feedback": "Explanation contradicts key." }
    ]
}
"""

try:
    # Call Gemini with Structured Output
    response = client.models.generate_content(
        model='gemini-3-flash-preview',
        contents=prompt,
        config={
            'response_mime_type': 'application/json',
            'response_schema': AuditResponse,
        }
    )
    
    res: AuditResponse = response.parsed
    
    # INJECT RESULTS BACK INTO UI
    for evaluation in res.evaluations:
        qid = evaluation.question_id
        
        # 1. Update Icon
        if qid in ai_status_icons:
            if evaluation.status == "PASS":
                ai_status_icons[qid].write("✅")
            else:
                ai_status_icons[qid].write("❌")
        
        # 2. Show Error Message (Only if Fail)
        if evaluation.status == "FAIL" and evaluation.feedback:
            if qid in ai_feedback_boxes:
                ai_feedback_boxes[qid].error(f"**AI:** {evaluation.feedback}")

except Exception as e:
    # Fail gracefully
    for q in questions:
        ai_status_icons[q.question_id].write("⚠️")
