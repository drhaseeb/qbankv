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
    /* Tighten container padding */
    [data-testid="stVerticalBlock"] > div {
        gap: 0.5rem;
    }
    div[data-testid="stExpander"] div[role="button"] p {
        font-weight: 600;
    }
    /* Style the Question Containers */
    .stElementContainer div[data-testid="stVerticalBlockBorderWrapper"] {
        padding: 1rem !important;
        margin-bottom: -1rem !important;
    }
    /* Buttons styling */
    div.stButton > button {
        width: 100%;
        border-radius: 6px;
        height: 36px;
        font-weight: 500;
        transition: all 0.2s;
    }
    div.stButton > button:hover {
        border-color: #0f9d58;
        color: #0f9d58;
    }
    div.stButton > button[kind="primary"] {
        background-color: #0f9d58; 
        border-color: #0f9d58;
    }
    /* Reference Fact Box */
    .fact-box {
        border-left: 5px solid #0f9d58;
        padding: 10px;
        border-radius: 4px;
        font-size: 0.95rem;
    }
</style>
""", unsafe_allow_html=True)

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_NAME = os.getenv("DB_NAME")
DB_PORT = 5432

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ==============================================================================
# 2. SESSION & MODELS
# ==============================================================================
if "skipped_groups" not in st.session_state:
    st.session_state["skipped_groups"] = []

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

# Store selected chapter in session to persist across re-runs
if "selected_chapter" not in st.session_state:
    st.session_state["selected_chapter"] = "All Chapters"

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
    chapter_name: str       # Added for context

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

def fetch_all_chapters():
    """Fetches unique chapter names for the dropdown."""
    conn = get_db()
    results = conn.run("SELECT DISTINCT chapter_name FROM question_bank ORDER BY chapter_name")
    conn.close()
    return [r[0] for r in results if r[0]] # Filter out None/Empty if any

def fetch_variant_group(skipped_ids, chapter_filter="All Chapters"):
    conn = get_db()
    
    # Base Filters (Unverified only)
    filters = ["(verification_status != 'verified' OR verification_status IS NULL)"]
    params = {}
    
    # 1. Apply Chapter Filter
    if chapter_filter != "All Chapters":
        filters.append("chapter_name = :chap")
        params['chap'] = chapter_filter
        
    # 2. Apply Skip List
    if skipped_ids:
        # pg8000 requires special syntax for list handling or separate logic
        # Here we use the clean ANY/ALL approach if supported, or manual construction
        filters.append("variant_group_id != ALL(:skip_list)")
        params['skip_list'] = skipped_ids

    where_clause = " AND ".join(filters)
    
    group_query = f"""
        SELECT DISTINCT variant_group_id 
        FROM question_bank 
        WHERE {where_clause}
        LIMIT 1
    """
    
    group_row = conn.run(group_query, **params)
    
    if not group_row:
        conn.close()
        return None, [], None

    target_group_id = group_row[0][0]

    # Fetch columns including chapter_name
    questions_query = """
        SELECT q.question_id, q.card_id, q.question_json, q.explanation, 
               q.variant_type, q.role, q.status, q.verification_status, 
               q.chapter_name, c.fact_text
        FROM question_bank q
        LEFT JOIN concept_cards c ON q.card_id = c.card_id
        WHERE q.variant_group_id = :gid 
        ORDER BY q.question_id ASC
    """
    rows = conn.run(questions_query, gid=target_group_id)
    questions = []
    shared_fact_text = "No reference fact found."
    if rows:
        shared_fact_text = rows[0][9]
    for row in rows:
        qid, c_id, q_json_str, q_expl, q_var, q_role, q_stat, q_verif, q_chap, _ = row
        q_json = json.loads(q_json_str) if isinstance(q_json_str, str) else q_json_str
        
        questions.append(QuestionData(
            question_id=qid, stem=q_json['stem'], options=q_json['options'],
            correct_key=q_json['correct_key'], explanation=q_expl, variant_type=q_var,
            role=q_role, status=q_stat, verification_status=q_verif or 'pending',
            chapter_name=q_chap
        ))
        
    conn.close()
    return target_group_id, questions, shared_fact_text

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

# --- CHAPTER FILTER (Top of Page) ---
# We fetch chapters only once or on login ideally, but here is fine for simplicity
all_chapters = fetch_all_chapters()
selected_chap = st.selectbox(
    "📂 Filter by Chapter", 
    ["All Chapters"] + all_chapters,
    index=0 if st.session_state["selected_chapter"] == "All Chapters" else (["All Chapters"] + all_chapters).index(st.session_state["selected_chapter"])
)

# Detect Change & Reset
if selected_chap != st.session_state["selected_chapter"]:
    st.session_state["selected_chapter"] = selected_chap
    st.session_state["group_data"] = None # Force re-fetch
    st.session_state["skipped_groups"] = [] # Optional: Clear skips when changing chapter? Usually yes.
    st.rerun()

# --- DATA LOADING ---
if 'group_data' not in st.session_state or st.session_state.group_data is None:
    st.session_state.group_data = fetch_variant_group(
        st.session_state["skipped_groups"], 
        st.session_state["selected_chapter"]
    )

group_id, questions, shared_fact = st.session_state.group_data if st.session_state.group_data else (None, [], None)

if not group_id:
    st.balloons()
    msg = f"All questions in '{selected_chap}' verified!"
    st.success(msg)
    if st.button("Reset Session / Check Other Chapters"):
        st.session_state["skipped_groups"] = []
        st.session_state["group_data"] = None
        st.rerun()
    st.stop()

# 1. REFERENCE FACT (Compact)
with st.expander("📖 View Reference Fact", expanded=True):
    st.markdown(f"<div class='fact-box'>{shared_fact}</div>", unsafe_allow_html=True)

question_feedback_map = {}

# 2. QUESTIONS LOOP
for q in questions:
    with st.container(border=True):
        # Header Row
        icon = "🔹" if q.role == "Primary" else "🔗"
        st.markdown(f"**{icon} {q.role}** • <small>{q.variant_type}</small>", unsafe_allow_html=True)

        question_feedback_map[q.question_id] = st.empty()

        edit_key = f"edit_{q.question_id}"
        
        if st.session_state.get(edit_key, False):
            # === EDIT MODE ===
            new_stem = st.text_area("Vignette", q.stem, height=100, key=f"stem_{q.question_id}")
            new_key = st.selectbox("Correct Option", ["A","B","C","D","E"], 
                                   index=["A","B","C","D","E"].index(q.correct_key),
                                   key=f"k_{q.question_id}")
            new_expl = st.text_area("Explanation", q.explanation, key=f"expl_{q.question_id}")
            
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
            st.markdown(f"**Vignette:** {q.stem}")
            
            # Compact Options
            opt_cols = st.columns(len(q.options))
            for i, opt in enumerate(q.options):
                with opt_cols[i]:
                    is_correct = opt['key'] == q.correct_key
                    color = "green" if is_correct else "none"
                    weight = "bold" if is_correct else "normal"
                    st.markdown(f"<div style='color:{color}; font-weight:{weight}; font-size:0.85rem;'>{opt['key']}) {opt['text']}</div>", unsafe_allow_html=True)
            
            with st.expander("Explanation Details"):
                st.markdown(f"<div class='explanation-text'>{q.explanation}</div>", unsafe_allow_html=True)

            # Row for Action Buttons
            f1, f2, f3 = st.columns([1, 1, 2])
            if f1.button("✏️ Edit", key=f"ed_{q.question_id}"):
                st.session_state[edit_key] = True
                st.rerun()
            
            tog_text = "🚫 Deactivate" if q.status == 'active' else "✅ Activate"
            if f2.button(tog_text, key=f"tg_{q.question_id}"):
                new_s = 'inactive' if q.status == 'active' else 'active'
                update_status_single(q.question_id, new_s)
                st.rerun()

# 3. AI VERDICT
global_verdict_container = st.empty()
global_verdict_container.info("⏳ AI is auditing these variants...")

# 4. BOTTOM BAR
st.divider()
bc1, bc2 = st.columns(2)
with bc1:
    if st.button("⏭️ Skip Group"):
        st.session_state["skipped_groups"].append(group_id)
        st.session_state.group_data = fetch_variant_group(st.session_state["skipped_groups"], st.session_state["selected_chapter"])
        st.rerun()
with bc2:
    if st.button("✅ Verify All", type="primary"):
        mark_group_verified(group_id)
        st.toast("Group Verified!")
        st.session_state.group_data = fetch_variant_group(st.session_state["skipped_groups"], st.session_state["selected_chapter"])
        st.rerun()

# ==============================================================================
# 6. AI INJECTION
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
