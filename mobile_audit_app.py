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
    [data-testid="stVerticalBlock"] > div { gap: 0.5rem; }
    
    /* Progress Bar Styling */
    .stProgress > div > div > div > div {
        background-color: #0f9d58;
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
        background-color: #f0f2f6;
    }
</style>
""", unsafe_allow_html=True)

# Environment Variables
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_NAME = os.getenv("DB_NAME")
DB_PORT = 5432
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)

# ==============================================================================
# 2. SESSION STATE INIT
# ==============================================================================
if "skipped_groups" not in st.session_state: st.session_state["skipped_groups"] = []
if "authenticated" not in st.session_state: st.session_state["authenticated"] = False
if "selected_chapter" not in st.session_state: st.session_state["selected_chapter"] = "All Chapters"
if "ai_result" not in st.session_state: st.session_state["ai_result"] = None
if "group_data" not in st.session_state: st.session_state["group_data"] = None

class QuestionAudit(BaseModel):
    question_id: int
    status: str       
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
    status: str             
    verification_status: str 
    chapter_name: str       

# ==============================================================================
# 3. DATABASE LOGIC
# ==============================================================================
def get_db():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(
        user=DB_USER, host=DB_HOST, password=st.session_state.get("db_password"), 
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

@st.cache_data(show_spinner=False)
def fetch_all_chapters():
    try:
        conn = get_db()
        results = conn.run("SELECT DISTINCT chapter_name FROM question_bank ORDER BY chapter_name")
        conn.close()
        return [r[0] for r in results if r[0]]
    except Exception:
        return []

@st.cache_data(show_spinner=False)
def fetch_progress(chapter_filter):
    """Calculates verified vs total groups. Cached until an action occurs."""
    conn = get_db()
    
    where_clause = ""
    params = {}
    
    if chapter_filter != "All Chapters":
        where_clause = "WHERE chapter_name = :chap"
        params['chap'] = chapter_filter

    # Count Total Groups
    total_q = f"SELECT COUNT(DISTINCT variant_group_id) FROM question_bank {where_clause}"
    total = conn.run(total_q, **params)[0][0] or 0
    
    # Count Verified Groups
    ver_clause = "WHERE verification_status = 'verified'"
    if where_clause:
        ver_clause += " AND chapter_name = :chap"
    else:
        pass # ver_clause is sufficient for All Chapters
        
    ver_q = f"SELECT COUNT(DISTINCT variant_group_id) FROM question_bank {ver_clause}"
    verified = conn.run(ver_q, **params)[0][0] or 0
    
    conn.close()
    return verified, total

def fetch_variant_group(skipped_ids, chapter_filter="All Chapters"):
    conn = get_db()
    
    filters = ["(verification_status != 'verified' OR verification_status IS NULL)"]
    params = {}
    
    if chapter_filter != "All Chapters":
        filters.append("chapter_name = :chap")
        params['chap'] = chapter_filter
        
    if skipped_ids:
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

# --- ACTION CALLBACKS (LOCKING LOGIC) ---
def verify_group_callback():
    """Executed on click, BEFORE rerun."""
    gid = st.session_state.get("current_group_id")
    if gid:
        conn = get_db()
        conn.run("UPDATE question_bank SET verification_status = 'verified' WHERE variant_group_id = :gid", gid=gid)
        conn.close()
        
        # Invalidate caches
        fetch_progress.clear()
        
        # Reset Session State for next load
        st.session_state["group_data"] = None
        st.session_state["ai_result"] = None
        st.toast("✅ Group Verified!")

def skip_group_callback():
    """Executed on click, BEFORE rerun."""
    gid = st.session_state.get("current_group_id")
    if gid:
        st.session_state["skipped_groups"].append(gid)
        st.session_state["group_data"] = None
        st.session_state["ai_result"] = None
        st.toast("⏭️ Group Skipped")

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

# --- CHAPTER FILTER ---
all_chapters = fetch_all_chapters()
selected_chap = st.selectbox(
    "📂 Filter by Chapter", 
    ["All Chapters"] + all_chapters,
    index=0 if st.session_state["selected_chapter"] == "All Chapters" else (["All Chapters"] + all_chapters).index(st.session_state["selected_chapter"])
)

if selected_chap != st.session_state["selected_chapter"]:
    st.session_state["selected_chapter"] = selected_chap
    st.session_state["skipped_groups"] = [] 
    st.session_state["group_data"] = None
    st.session_state["ai_result"] = None
    st.rerun()

# --- PROGRESS BAR ---
verified_count, total_count = fetch_progress(selected_chap)
if total_count > 0:
    progress_val = verified_count / total_count
    st.progress(progress_val, text=f"Progress: {verified_count}/{total_count} Groups Verified ({int(progress_val*100)}%)")
else:
    st.info("No groups found for this chapter.")

# --- DATA LOADING ---
if 'group_data' not in st.session_state or st.session_state.group_data is None:
    with st.spinner("⏳ Loading next question batch..."):
        st.session_state.group_data = fetch_variant_group(
            st.session_state["skipped_groups"], 
            st.session_state["selected_chapter"]
        )
        st.session_state["ai_result"] = None

group_id, questions, shared_fact = st.session_state.group_data if st.session_state.group_data else (None, [], None)

# Save current ID for callbacks
st.session_state["current_group_id"] = group_id

if not group_id:
    st.balloons()
    st.success(f"All questions in '{selected_chap}' verified!")
    if st.button("Reset Session"):
        st.session_state["skipped_groups"] = []
        st.session_state["group_data"] = None
        st.rerun()
    st.stop()

# 1. REFERENCE FACT
with st.expander("📖 View Reference Fact", expanded=True):
    st.markdown(f"<div class='fact-box'>{shared_fact}</div>", unsafe_allow_html=True)

question_feedback_map = {}

# 2. QUESTIONS LOOP
for q in questions:
    with st.container(border=True):
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
                st.session_state["ai_result"] = None # Reset AI
                st.session_state.group_data = fetch_variant_group(st.session_state["skipped_groups"], st.session_state["selected_chapter"])
                st.rerun()
            if b2.button("Cancel", key=f"cn_{q.question_id}"):
                st.session_state[edit_key] = False
                st.rerun()
        else:
            # === READ MODE ===
            st.markdown(f"**Vignette:** {q.stem}")
            
            opt_cols = st.columns(len(q.options))
            for i, opt in enumerate(q.options):
                with opt_cols[i]:
                    is_correct = opt['key'] == q.correct_key
                    color = "green" if is_correct else "gray"
                    weight = "bold" if is_correct else "normal"
                    st.markdown(f"<div style='color:{color}; font-weight:{weight}; font-size:0.85rem;'>{opt['key']}) {opt['text']}</div>", unsafe_allow_html=True)
            
            with st.expander("Explanation Details"):
                st.markdown(f"<div class='explanation-text'>{q.explanation}</div>", unsafe_allow_html=True)

            f1, f2, f3 = st.columns([1, 1, 2])
            if f1.button("✏️ Edit", key=f"ed_{q.question_id}"):
                st.session_state[edit_key] = True
                st.rerun()
            
            tog_text = "🚫 Deactivate" if q.status == 'active' else "✅ Activate"
            if f2.button(tog_text, key=f"tg_{q.question_id}"):
                new_s = 'inactive' if q.status == 'active' else 'active'
                update_status_single(q.question_id, new_s)
                q.status = new_s
                st.rerun()

# 3. GLOBAL ACTIONS
st.divider()
bc1, bc2 = st.columns(2)
with bc1:
    # USING CALLBACK FOR LOCKING
    st.button("⏭️ Skip Group", on_click=skip_group_callback)
with bc2:
    # USING CALLBACK FOR LOCKING
    st.button("✅ Verify All", type="primary", on_click=verify_group_callback)

# ==============================================================================
# 6. LAZY AI INJECTION
# ==============================================================================
ai_placeholder = st.empty()

if st.session_state["ai_result"]:
    res = st.session_state["ai_result"]
    if res.global_verdict == "PASS":
        ai_placeholder.success("✅ **AI VERDICT: PASS**")
    else:
        ai_placeholder.error(f"❌ **AI VERDICT: FAIL** - {res.global_summary}")
    
    for ev in res.evaluations:
        if ev.status == "FAIL" and ev.question_id in question_feedback_map:
            question_feedback_map[ev.question_id].error(f"**AI Insight:** {ev.feedback}")
else:
    with st.status("🤖 AI Auditor is analyzing...", expanded=True) as status:
        prompt = "Audit this medical question set (FCPS Part 1). Focus ONLY on factual accuracy.\n\n"
        prompt += f"Reference Fact (Context): {shared_fact}\n\n"

        for q in questions:
            opts_str = ", ".join([f"{o['key']}:{o['text']}" for o in q.options])
            prompt += f"--- QUESTION ID {q.question_id} ---\n"
            prompt += f"Stem: {q.stem}\nOptions: {opts_str}\nKey: {q.correct_key}\nExpl: {q.explanation}\n\n"

        prompt += """
        Check for: 
        1. Factually incorrect medical statements.
        2. Mismatch between Explanation and Key.
        3. Logic errors.

        OUTPUT JSON FORMAT:
        {
            "global_verdict": "PASS" or "FAIL",
            "global_summary": "Short note if FAIL, null if PASS",
            "evaluations": [
                { "question_id": 123, "status": "FAIL", "feedback": "Brief feedback." },
                { "question_id": 456, "status": "PASS", "feedback": null }
            ]
        }
        """
        
        try:
            response = client.models.generate_content(
                model='gemini-2.0-flash', 
                contents=prompt,
                config={'response_mime_type': 'application/json', 'response_schema': AuditResponse}
            )
            res = response.parsed
            st.session_state["ai_result"] = res
            status.update(label="✅ AI Audit Complete", state="complete", expanded=False)
            st.rerun()
        except Exception as e:
            status.update(label="⚠️ AI Audit Failed", state="error")
            st.error(f"AI Error: {e}")
if "selected_chapter" not in st.session_state:
    st.session_state["selected_chapter"] = "All Chapters"
if "ai_result" not in st.session_state:
    st.session_state["ai_result"] = None

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
    status: str             
    verification_status: str 
    chapter_name: str       

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

@st.cache_data(show_spinner=False)
def fetch_all_chapters(_password_placeholder):
    """Fetches unique chapter names. Cached to improve reload speed."""
    try:
        conn = get_db()
        results = conn.run("SELECT DISTINCT chapter_name FROM question_bank ORDER BY chapter_name")
        conn.close()
        return [r[0] for r in results if r[0]]
    except Exception:
        return []

def fetch_variant_group(skipped_ids, chapter_filter="All Chapters"):
    conn = get_db()
    
    filters = ["(verification_status != 'verified' OR verification_status IS NULL)"]
    params = {}
    
    if chapter_filter != "All Chapters":
        filters.append("chapter_name = :chap")
        params['chap'] = chapter_filter
        
    if skipped_ids:
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

def clear_group_state():
    """Helper to reset state and force a new fetch."""
    st.session_state["group_data"] = None
    st.session_state["ai_result"] = None

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

# --- CHAPTER FILTER ---
# Pass a dummy arg to cache_data so it knows to re-run if password changes (unlikely but safe)
all_chapters = fetch_all_chapters(st.session_state["db_password"])
selected_chap = st.selectbox(
    "📂 Filter by Chapter", 
    ["All Chapters"] + all_chapters,
    index=0 if st.session_state["selected_chapter"] == "All Chapters" else (["All Chapters"] + all_chapters).index(st.session_state["selected_chapter"])
)

if selected_chap != st.session_state["selected_chapter"]:
    st.session_state["selected_chapter"] = selected_chap
    st.session_state["skipped_groups"] = [] 
    clear_group_state()
    st.rerun()

# --- DATA LOADING WITH SPINNER ---
# This ensures the UI is "locked" with a spinner while fetching
if 'group_data' not in st.session_state or st.session_state.group_data is None:
    with st.spinner("⏳ Fetching next question batch..."):
        st.session_state.group_data = fetch_variant_group(
            st.session_state["skipped_groups"], 
            st.session_state["selected_chapter"]
        )
        # Reset AI result whenever new data loads
        st.session_state["ai_result"] = None

group_id, questions, shared_fact = st.session_state.group_data if st.session_state.group_data else (None, [], None)

if not group_id:
    st.balloons()
    st.success(f"All questions in '{selected_chap}' verified!")
    if st.button("Reset Session"):
        st.session_state["skipped_groups"] = []
        clear_group_state()
        st.rerun()
    st.stop()

# 1. REFERENCE FACT
with st.expander("📖 View Reference Fact", expanded=True):
    st.markdown(f"<div class='fact-box'>{shared_fact}</div>", unsafe_allow_html=True)

# Placeholder map to inject AI errors later
question_feedback_map = {}

# 2. QUESTIONS LOOP
for q in questions:
    with st.container(border=True):
        icon = "🔹" if q.role == "Primary" else "🔗"
        st.markdown(f"**{icon} {q.role}** • <small>{q.variant_type}</small>", unsafe_allow_html=True)

        # Create a placeholder for AI error messages immediately below the header
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
                # Invalidate AI result so it re-runs on the edited text
                st.session_state["ai_result"] = None
                # We do NOT clear group_data here, we just rerun to refresh this specific question
                st.session_state.group_data = fetch_variant_group(st.session_state["skipped_groups"], st.session_state["selected_chapter"])
                st.rerun()
            if b2.button("Cancel", key=f"cn_{q.question_id}"):
                st.session_state[edit_key] = False
                st.rerun()
        else:
            # === READ MODE ===
            st.markdown(f"**Vignette:** {q.stem}")
            
            opt_cols = st.columns(len(q.options))
            for i, opt in enumerate(q.options):
                with opt_cols[i]:
                    is_correct = opt['key'] == q.correct_key
                    color = "green" if is_correct else "gray"
                    weight = "bold" if is_correct else "normal"
                    st.markdown(f"<div style='color:{color}; font-weight:{weight}; font-size:0.85rem;'>{opt['key']}) {opt['text']}</div>", unsafe_allow_html=True)
            
            with st.expander("Explanation Details"):
                st.markdown(f"<div class='explanation-text'>{q.explanation}</div>", unsafe_allow_html=True)

            f1, f2, f3 = st.columns([1, 1, 2])
            if f1.button("✏️ Edit", key=f"ed_{q.question_id}"):
                st.session_state[edit_key] = True
                st.rerun()
            
            tog_text = "🚫 Deactivate" if q.status == 'active' else "✅ Activate"
            if f2.button(tog_text, key=f"tg_{q.question_id}"):
                new_s = 'inactive' if q.status == 'active' else 'active'
                update_status_single(q.question_id, new_s)
                # We update the local object so we don't need a full DB refetch
                q.status = new_s
                st.rerun()

# 3. GLOBAL ACTIONS
st.divider()
bc1, bc2 = st.columns(2)
with bc1:
    if st.button("⏭️ Skip Group"):
        st.session_state["skipped_groups"].append(group_id)
        clear_group_state() # Clear state to trigger spinner on rerun
        st.rerun()
with bc2:
    if st.button("✅ Verify All", type="primary"):
        mark_group_verified(group_id)
        st.toast("Group Verified!")
        clear_group_state() # Clear state to trigger spinner on rerun
        st.rerun()

# ==============================================================================
# 6. LAZY AI INJECTION
# ==============================================================================
# This section runs after the UI is drawn. 
# We check if we already have a cached result to avoid re-running on simple toggles.

ai_placeholder = st.empty()

if st.session_state["ai_result"]:
    # == RENDER CACHED RESULTS ==
    res = st.session_state["ai_result"]
    if res.global_verdict == "PASS":
        ai_placeholder.success("✅ **AI VERDICT: PASS**")
    else:
        ai_placeholder.error(f"❌ **AI VERDICT: FAIL** - {res.global_summary}")
    
    # Inject errors into specific question containers
    for ev in res.evaluations:
        if ev.status == "FAIL" and ev.question_id in question_feedback_map:
            question_feedback_map[ev.question_id].error(f"**AI Insight:** {ev.feedback}")

else:
    # == RUN NEW AUDIT (LAZY) ==
    # Using st.status creates a nice container that doesn't block previous UI from being visible
    with st.status("🤖 AI Auditor is analyzing...", expanded=True) as status:
        prompt = "Audit this medical question set (FCPS Part 1). Focus ONLY on factual accuracy.\n\n"
        prompt += f"Reference Fact (Context): {shared_fact}\n\n"

        for q in questions:
            opts_str = ", ".join([f"{o['key']}:{o['text']}" for o in q.options])
            prompt += f"--- QUESTION ID {q.question_id} ---\n"
            prompt += f"Stem: {q.stem}\nOptions: {opts_str}\nKey: {q.correct_key}\nExpl: {q.explanation}\n\n"

        prompt += """
        Check for: 
        1. Factually incorrect medical statements.
        2. Mismatch between Explanation and Key.
        3. Logic errors.

        OUTPUT JSON FORMAT:
        {
            "global_verdict": "PASS" or "FAIL",
            "global_summary": "Short note if FAIL, null if PASS",
            "evaluations": [
                { "question_id": 123, "status": "FAIL", "feedback": "Brief feedback." },
                { "question_id": 456, "status": "PASS", "feedback": null }
            ]
        }
        """
        
        try:
            response = client.models.generate_content(
                model='gemini-2.0-flash', # Use a fast model
                contents=prompt,
                config={
                    'response_mime_type': 'application/json',
                    'response_schema': AuditResponse,
                }
            )
            res = response.parsed
            st.session_state["ai_result"] = res
            
            status.update(label="✅ AI Audit Complete", state="complete", expanded=False)
            st.rerun() # Rerun once to inject the results into the specific placeholders above

        except Exception as e:
            status.update(label="⚠️ AI Audit Failed", state="error")
            st.error(f"AI Error: {e}")
