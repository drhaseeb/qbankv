import streamlit as st
import pg8000.native
import ssl
import json
import google.generativeai as genai
from dataclasses import dataclass
from typing import List, Dict
import os

# ==============================================================================
# 1. CONFIGURATION (No Password Here!)
# ==============================================================================
# We only store non-sensitive connection details
DB_HOST = os.getenv("DB_HOST", "psql-qbank-core-01.postgres.database.azure.com")
DB_USER = os.getenv("DB_USER", "rmhadmin")
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_PORT = 5432

# API Key still needs to be in env vars, or you can ask for it in login too
genai.configure(api_key=os.getenv("GEMINI_API_KEY", "YOUR_API_KEY"))

# ==============================================================================
# 2. LOGIN & CONNECTION LOGIC
# ==============================================================================
def try_connect(password):
    """Attempts to connect to DB to verify credentials."""
    try:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        
        conn = pg8000.native.Connection(
            user=DB_USER,
            host=DB_HOST,
            password=password, # Test the input password
            database=DB_NAME,
            port=DB_PORT,
            ssl_context=ssl_ctx
        )
        conn.close()
        return True, None
    except Exception as e:
        return False, str(e)

def login_screen():
    """Renders the login form and stops execution if not logged in."""
    
    # If we already have a valid password in memory, skip login
    if st.session_state.get("authenticated", False):
        return

    st.markdown("## 🔐 Database Login")
    st.markdown("Enter your PostgreSQL password to unlock the app.")
    
    password_input = st.text_input("Database Password", type="password")
    
    if st.button("Login"):
        with st.spinner("Authenticating with Database..."):
            is_valid, error = try_connect(password_input)
            
            if is_valid:
                st.session_state["db_password"] = password_input
                st.session_state["authenticated"] = True
                st.success("✅ Connected!")
                st.rerun()
            else:
                st.error(f"❌ Connection Failed: {error}")
                
    # Stop the app here if not authenticated
    st.stop()

# ==============================================================================
# 3. DB HELPER (Uses the Session Password)
# ==============================================================================
def get_db():
    """Returns a connection using the password stored in session state."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    # CRITICAL: Use the password from the session
    return pg8000.native.Connection(
        user=DB_USER,
        host=DB_HOST,
        password=st.session_state["db_password"], 
        database=DB_NAME,
        port=DB_PORT,
        ssl_context=ssl_ctx
    )

# ==============================================================================
# 🚀 APP START
# ==============================================================================
st.set_page_config(page_title="FCPS Secure Audit", layout="centered")

# 1. RUN LOGIN CHECK FIRST
login_screen()

# ==============================================================================
# 2. DATA MODELS
# ==============================================================================
@dataclass
class QuestionData:
    question_id: int # <--- RENAMED TO MATCH DB
    stem: str
    options: List[Dict]
    correct_key: str
    explanation: str
    variant_type: str
    role: str
    status: str

def fetch_variant_group():
    """Fetches ALL questions belonging to a single unverified Variant Group."""
    conn = get_db()
    
    # 1. Find a Variant Group ID that needs work (is not fully verified)
    group_query = """
        SELECT DISTINCT variant_group_id 
        FROM question_bank 
        WHERE status != 'verified' 
        LIMIT 1
    """
    group_row = conn.run(group_query)
    
    if not group_row:
        conn.close()
        return None, []

    target_group_id = group_row[0][0]

    # 2. Fetch ALL questions in this group (Primaries AND Clones together)
    # FIX: Using 'question_id' instead of 'id'
    questions_query = """
        SELECT question_id, question_json, explanation, variant_type, role, status
        FROM question_bank 
        WHERE variant_group_id = :gid
        ORDER BY question_id ASC
    """
    rows = conn.run(questions_query, gid=target_group_id)
    
    questions = []
    for row in rows:
        qid, q_json_str, q_expl, q_var, q_role, q_stat = row
        q_json = json.loads(q_json_str) if isinstance(q_json_str, str) else q_json_str
        
        q_obj = QuestionData(
            question_id=qid, # <--- Mapping DB column to Object
            stem=q_json['stem'],
            options=q_json['options'],
            correct_key=q_json['correct_key'],
            explanation=q_expl,
            variant_type=q_var,
            role=q_role,
            status=q_stat
        )
        questions.append(q_obj)

    conn.close()
    return target_group_id, questions

def save_edit(qid, new_json, new_expl):
    conn = get_db()
    # FIX: Using 'question_id'
    conn.run("UPDATE question_bank SET question_json = :qj, explanation = :ex WHERE question_id = :qid", 
             qj=json.dumps(new_json), ex=new_expl, qid=qid)
    conn.close()

def update_status_single(qid, new_status):
    conn = get_db()
    # FIX: Using 'question_id'
    conn.run("UPDATE question_bank SET status = :s WHERE question_id = :qid", s=new_status, qid=qid)
    conn.close()

def mark_group_verified(group_id):
    conn = get_db()
    # Group logic remains same (updates all questions with this Group ID)
    conn.run("UPDATE question_bank SET status = 'verified' WHERE variant_group_id = :gid", gid=group_id)
    conn.close()

# ==============================================================================
# 3. AI AUDIT ENGINE
# ==============================================================================
def audit_group_with_gemini(questions: List[QuestionData]):
    model = genai.GenerativeModel('gemini-3-flash-preview')
    
    prompt = "You are a Medical Examiner auditing FCPS Part 1 questions. Review this SET of related questions.\n\n"
    
    for i, q in enumerate(questions):
        prompt += f"--- QUESTION {i+1} ({q.role}: {q.variant_type}) ---\n"
        prompt += f"ID: {q.question_id}\nStem: {q.stem}\nKey: {q.correct_key}\nExplanation: {q.explanation}\n\n"

    prompt += """
    TASK: Verify medical accuracy for EACH question.
    
    OUTPUT JSON FORMAT:
    {
        "overall_verdict": "PASS" or "FAIL",
        "summary": "Brief summary of the group's quality.",
        "evaluations": [
            {"index": 1, "status": "PASS", "feedback": "Correct."},
            {"index": 2, "status": "FAIL", "feedback": "Incorrect dose mentioned."}
        ]
    }
    """
    
    try:
        res = model.generate_content(prompt)
        text = res.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except:
        return {"overall_verdict": "ERROR", "summary": "AI Failed", "evaluations": []}

# ==============================================================================
# 4. APP UI
# ==============================================================================
st.set_page_config(page_title="FCPS Group Audit", layout="centered")

if 'group_data' not in st.session_state:
    st.session_state.group_data = fetch_variant_group()
    st.session_state.audit_result = None

group_id, questions = st.session_state.group_data

if not group_id:
    st.success("🎉 Database is clean! No unverified groups found.")
    if st.button("Refresh"):
        st.cache_data.clear()
        st.session_state.group_data = fetch_variant_group()
        st.rerun()
else:
    # --- AUTO AUDIT (ON LOAD) ---
    if st.session_state.audit_result is None:
        with st.spinner("🤖 Auditing Concept Cluster..."):
            st.session_state.audit_result = audit_group_with_gemini(questions)

    # --- TOP BAR ---
    st.progress(100, text=f"Reviewing Group: {group_id}")
    
    audit = st.session_state.audit_result
    if audit:
        color = "green" if audit.get("overall_verdict") == "PASS" else "red"
        st.markdown(f":{color}-background[**AI Verdict: {audit.get('overall_verdict')}**]")
        st.caption(audit.get("summary"))

    # --- RENDER QUESTIONS LIST ---
    for i, q in enumerate(questions):
        
        # Get specific AI feedback
        evals = audit.get("evaluations", [])
        q_feedback = next((e for e in evals if e.get("index") == i + 1), {})
        ai_stat = q_feedback.get("status", "Unknown")
        ai_note = q_feedback.get("feedback", "")
        
        # UI Card
        with st.container(border=True):
            # Header Row
            c1, c2, c3 = st.columns([0.6, 0.2, 0.2])
            with c1:
                icon = "🔹" if q.role == "Primary" else "Zw"
                st.markdown(f"**{icon} {q.role}** ({q.variant_type})")
            with c2:
                scolor = "green" if q.status == 'active' else "red"
                st.markdown(f":{scolor}[{q.status}]")
            with c3:
                aicolor = "✅" if ai_stat == "PASS" else "❌"
                st.write(aicolor)

            if ai_stat == "FAIL":
                st.error(f"AI: {ai_note}")

            # Editable Area Check
            edit_key = f"edit_mode_{q.question_id}"
            
            if st.session_state.get(edit_key, False):
                # EDIT MODE
                new_stem = st.text_area("Stem", q.stem, key=f"s_{q.question_id}")
                new_expl = st.text_area("Explanation", q.explanation, key=f"e_{q.question_id}")
                new_key = st.selectbox("Key", ["A","B","C","D","E"], index=["A","B","C","D","E"].index(q.correct_key), key=f"k_{q.question_id}")
                
                if st.button("💾 Save", key=f"btn_save_{q.question_id}"):
                    new_json = {"stem": new_stem, "options": q.options, "correct_key": new_key}
                    save_edit(q.question_id, new_json, new_expl)
                    st.session_state[edit_key] = False
                    st.rerun()
            else:
                # READ MODE
                st.write(q.stem)
                with st.expander(f"Answer: {q.correct_key} (Click to see Explanation)"):
                    st.info(q.explanation)
                
                # Tools
                b1, b2 = st.columns(2)
                if b1.button("✏️ Edit", key=f"ed_{q.question_id}"):
                    st.session_state[edit_key] = True
                    st.rerun()
                
                toggle_label = "Deactivate" if q.status == 'active' else "Activate"
                if b2.button(toggle_label, key=f"tog_{q.question_id}"):
                    new_s = 'inactive' if q.status == 'active' else 'active'
                    update_status_single(q.question_id, new_s)
                    st.rerun()

    # --- FOOTER ACTIONS ---
    st.divider()
    fc1, fc2 = st.columns(2)
    
    if fc1.button("⏭️ Skip (Keep Pending)"):
        st.session_state.audit_result = None
        st.session_state.group_data = fetch_variant_group()
        st.rerun()
        
    if fc2.button("✅ Approve Group", type="primary"):
        mark_group_verified(group_id)
        st.toast("Group Verified!")
        st.session_state.audit_result = None
        st.session_state.group_data = fetch_variant_group()
        st.rerun()
