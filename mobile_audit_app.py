import streamlit as st
import pg8000.native
import ssl
import json
import google.generativeai as genai
from dataclasses import dataclass
from typing import List, Dict

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
# Replace with actual credentials or use st.secrets
DB_CONFIG = {
    "host": "psql-qbank-core-01.postgres.database.azure.com",
    "user": "rmhadmin",
    "password": "Password",
    "database": "postgres",
    "port": 5432
}
GOOGLE_API_KEY = "Key"

genai.configure(api_key=GOOGLE_API_KEY)

# ==============================================================================
# 2. DATABASE & DATA MODELS
# ==============================================================================
@dataclass
class QuestionData:
    id: int
    stem: str
    options: List[Dict]
    correct_key: str
    explanation: str
    variant_type: str
    role: str
    status: str

def get_db():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(**DB_CONFIG, ssl_context=ssl_ctx)

def fetch_concept_cluster():
    """Fetches a Primary question and its Clones to review as a single concept."""
    conn = get_db()
    # Fetch a random Primary question that hasn't been 'verified' yet
    primary_row = conn.run("""
        SELECT id, question_json, explanation, variant_type, variant_group_id, role, status
        FROM question_bank 
        WHERE role = 'Primary' AND status != 'verified' 
        ORDER BY created_at DESC 
        LIMIT 1
    """)
    
    if not primary_row:
        conn.close()
        return None, None

    pid, p_json_str, p_expl, p_var, group_id, p_role, p_stat = primary_row[0]
    p_json = json.loads(p_json_str) if isinstance(p_json_str, str) else p_json_str
    
    primary_q = QuestionData(pid, p_json['stem'], p_json['options'], p_json['correct_key'], p_expl, p_var, p_role, p_stat)

    # Fetch Clones
    clones_rows = conn.run("""
        SELECT id, question_json, explanation, variant_type, role, status
        FROM question_bank 
        WHERE variant_group_id = :gid AND role != 'Primary'
    """, gid=group_id)
    
    clones = []
    for row in clones_rows:
        cid, c_json_str, c_expl, c_var, c_role, c_stat = row
        c_json = json.loads(c_json_str) if isinstance(c_json_str, str) else c_json_str
        clones.append(QuestionData(cid, c_json['stem'], c_json['options'], c_json['correct_key'], c_expl, c_var, c_role, c_stat))

    conn.close()
    return primary_q, clones

def update_question_status(q_id, new_status):
    conn = get_db()
    conn.run("UPDATE question_bank SET status = :s WHERE id = :id", s=new_status, id=q_id)
    conn.close()

def save_edit(q_id, new_json, new_expl):
    conn = get_db()
    conn.run("UPDATE question_bank SET question_json = :qj, explanation = :ex WHERE id = :id", 
             qj=json.dumps(new_json), ex=new_expl, id=q_id)
    conn.close()

# ==============================================================================
# 3. AI VERIFICATION ENGINE
# ==============================================================================
def run_auto_verification(primary: QuestionData, clones: List[QuestionData]):
    """Sends content to Gemini Flash for a truth audit."""
    model = genai.GenerativeModel('gemini-3-flash-preview')
    
    content_block = f"PRIMARY CONCEPT ({primary.variant_type}):\n{primary.stem}\nAnswer: {primary.correct_key}\nExplanation: {primary.explanation}\n\n"
    for i, c in enumerate(clones):
        content_block += f"VARIANT {i+1} ({c.variant_type}):\n{c.stem}\nAnswer: {c.correct_key}\nExplanation: {c.explanation}\n\n"

    prompt = f"""
    Act as a Strict Medical Examiner for FCPS Part 1. 
    Audit the following medical concept cluster (a primary question and its variations) for factual accuracy and logic.

    {content_block}

    OUTPUT IN JSON ONLY:
    {{
        "verdict": "ACCURATE" or "INACCURATE",
        "confidence_score": 0-100,
        "summary": "Brief summary of accuracy. (only if inaccurate)",
        "mistakes": [
            "List specific errors found here. If none, leave empty."
        ]
    }}
    """
    try:
        response = model.generate_content(prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except:
        return {"verdict": "ERROR", "summary": "AI connection failed.", "mistakes": []}

# ==============================================================================
# 4. UI COMPONENTS
# ==============================================================================
def render_question_card(q: QuestionData, idx: str):
    """Displays a single question block with Edit/Status controls."""
    with st.container(border=True):
        # Header
        col1, col2 = st.columns([0.8, 0.2])
        with col1:
            st.markdown(f"**{q.role}**: {q.variant_type}")
        with col2:
            # Status Indicator
            color = "green" if q.status == 'active' else "red"
            st.markdown(f":{color}[{q.status.upper()}]")

        # Read Mode vs Edit Mode
        if st.session_state.get(f"edit_{q.id}", False):
            # --- EDIT MODE ---
            new_stem = st.text_area("Stem", q.stem, key=f"stem_{q.id}")
            new_expl = st.text_area("Explanation", q.explanation, key=f"expl_{q.id}")
            
            # Simple Key Editor
            new_key = st.selectbox("Correct Key", ["A","B","C","D","E"], 
                                   index=["A","B","C","D","E"].index(q.correct_key), 
                                   key=f"key_{q.id}")
            
            c1, c2 = st.columns(2)
            if c1.button("💾 Save", key=f"save_{q.id}"):
                # Reconstruct JSON with new stem/key (options editing omitted for brevity but can be added)
                new_q_json = {"stem": new_stem, "options": q.options, "correct_key": new_key}
                save_edit(q.id, new_q_json, new_expl)
                st.session_state[f"edit_{q.id}"] = False
                st.rerun()
            
            if c2.button("Cancel", key=f"cancel_{q.id}"):
                st.session_state[f"edit_{q.id}"] = False
                st.rerun()
        else:
            # --- READ MODE ---
            st.write(q.stem)
            
            # Show Options
            for opt in q.options:
                prefix = "✅" if opt['key'] == q.correct_key else "⚪"
                st.text(f"{prefix} {opt['key']}) {opt['text']}")
            
            st.info(f"**Explanation:** {q.explanation}")
            
            # Toolbar
            btn_col1, btn_col2 = st.columns(2)
            if btn_col1.button("✏️ Edit", key=f"btn_edit_{q.id}"):
                st.session_state[f"edit_{q.id}"] = True
                st.rerun()
            
            # Toggle Active/Inactive
            btn_label = "Deactivate 🚫" if q.status == 'active' else "Activate ✅"
            new_stat = 'inactive' if q.status == 'active' else 'active'
            if btn_col2.button(btn_label, key=f"btn_stat_{q.id}"):
                update_question_status(q.id, new_stat)
                st.rerun()

# ==============================================================================
# 5. MAIN APP LOGIC
# ==============================================================================
st.set_page_config(page_title="FCPS Auto-Audit", layout="centered", page_icon="⚡")

# 1. Load Data
if 'audit_data' not in st.session_state:
    st.session_state.audit_data = fetch_concept_cluster()
    st.session_state.ai_audit_result = None

primary, clones = st.session_state.audit_data

if not primary:
    st.success("✅ No pending questions found!")
    if st.button("Reload"):
        st.cache_data.clear()
        st.session_state.audit_data = fetch_concept_cluster()
        st.rerun()
else:
    # 2. Trigger Auto-Verification (Once per load)
    if st.session_state.ai_audit_result is None:
        with st.spinner("🤖 Gemini is auditing this concept..."):
            st.session_state.ai_audit_result = run_auto_verification(primary, clones)

    # 3. Display Content
    st.title("⚡ Rapid Review")
    
    # Render Questions
    render_question_card(primary, "p")
    for i, c in enumerate(clones):
        render_question_card(c, str(i))

    # 4. Display AI Verdict (Sticky Bottom or End of Page)
    st.divider()
    audit = st.session_state.ai_audit_result
    
    if audit:
        # Determine Color
        if audit.get("verdict") == "ACCURATE":
            box_color = "green"
            icon = "✅"
        else:
            box_color = "red"
            icon = "⚠️"
            
        st.subheader(f"{icon} AI Verdict: {audit.get('verdict')}")
        st.write(f"**Summary:** {audit.get('summary')}")
        
        if audit.get("mistakes"):
            st.error("🚨 **Detected Issues:**")
            for m in audit["mistakes"]:
                st.write(f"- {m}")
    
    # 5. Final Actions
    st.markdown("---")
    c1, c2 = st.columns(2)
    
    if c1.button("⏭️ Next Concept (Keep Pending)", use_container_width=True):
        # Just move on, don't mark verified
        st.session_state.ai_audit_result = None
        st.session_state.audit_data = fetch_concept_cluster()
        st.rerun()
        
    if c2.button("✅ Verify All & Next", type="primary", use_container_width=True):
        # Mark all as verified
        conn = get_db()
        ids = [primary.id] + [c.id for c in clones]
        for i in ids:
            conn.run("UPDATE question_bank SET status = 'verified' WHERE id = :id", id=i)
        conn.close()
        
        st.toast("Verified!")
        st.session_state.ai_audit_result = None
        st.session_state.audit_data = fetch_concept_cluster()
        st.rerun()
