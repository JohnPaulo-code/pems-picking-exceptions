"""
PEMS captain dashboard.

Run:  streamlit run dashboard.py

Reads picking_events, scan_attempts, quality_tasks, quality_task_outcomes.
Writes only to quality_tasks and quality_task_outcomes (captain + shopper
actions). Detection data is never modified.
"""

import io
import random
from datetime import datetime, timezone

import psycopg2
import streamlit as st

from pems.config import DB_PARAMS
from pems.features import fetch_features
from pems.scoring import score_locations
from pems.tasks import build_reason
from pems.export import write_excel, fetch_workflow_state


st.set_page_config(page_title="PEMS Captain Dashboard", layout="wide")


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=5)
def list_runs():
    conn = psycopg2.connect(**DB_PARAMS)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT run_tag, started_at, status
                FROM simulation_runs ORDER BY started_at DESC;
            """)
            return cur.fetchall()
    finally:
        conn.close()


@st.cache_data(ttl=5)
def load_data(run_tag):
    conn = psycopg2.connect(**DB_PARAMS)
    try:
        ranked = score_locations(fetch_features(run_tag, conn))
        workflow = fetch_workflow_state(conn, run_tag)
    finally:
        conn.close()
    return ranked, workflow


# ---------------------------------------------------------------------------
# Per-task write helpers
# ---------------------------------------------------------------------------
def accept_task(conn, task_id):
    """Accept a single pending task: assign a random shopper, move to in_progress."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE quality_tasks
            SET status = 'in_progress',
                assigned_shopper_id = (
                    SELECT employee_id FROM employees
                    WHERE employee_role = 'quality_shopper'
                    ORDER BY random() LIMIT 1
                )
            WHERE task_id = %s AND status = 'pending'
            RETURNING assigned_shopper_id;
        """, (task_id,))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None


def reject_task(conn, task_id, reason):
    """Reject a pending task: mark cancelled, record reason in notes."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE quality_tasks
            SET status = 'cancelled',
                task_reason = task_reason || '  [Rejected: ' || %s || ']'
            WHERE task_id = %s AND status = 'pending';
        """, (reason, task_id))
        conn.commit()


def complete_task(conn, task_id, run_tag, confirmed_cause, notes):
    """Record a shopper outcome: insert into quality_task_outcomes, mark completed.

    resolved_at is anchored inside the simulated timeline so recurrence is
    meaningful (2 days before the last event at this location).
    """
    with conn.cursor() as cur:
        # Find location for this task
        cur.execute("SELECT location_id FROM quality_tasks WHERE task_id = %s;",
                    (task_id,))
        row = cur.fetchone()
        if not row:
            return False
        loc_id = row[0]

        # Anchor resolution within the simulated timeline
        cur.execute("""
            SELECT MAX(pe.event_time) - INTERVAL '2 days'
            FROM picking_events pe
            JOIN simulation_event_labels l ON l.event_id = pe.event_id
            WHERE l.run_tag = %s AND pe.location_id = %s;
        """, (run_tag, loc_id))
        r = cur.fetchone()
        resolved_at = r[0] if r and r[0] else datetime.now(timezone.utc)

        cur.execute("""
            INSERT INTO quality_task_outcomes
                (task_id, confirmed_cause, resolution_notes, resolved_at)
            VALUES (%s, %s, %s, %s);
        """, (task_id, confirmed_cause, notes, resolved_at))

        cur.execute("""
            UPDATE quality_tasks SET status = 'completed'
            WHERE task_id = %s;
        """, (task_id,))
        conn.commit()
        return True


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.header("Filters")
runs = list_runs()
run_tags = [r[0] for r in runs]
if not run_tags:
    st.error("No simulation runs found. Run generate_pems_data.py first.")
    st.stop()
run_tag = st.sidebar.selectbox("Run", run_tags, index=0)
run_status_map = {r[0]: r[2] for r in runs}
st.sidebar.markdown(f"**Run status:** {run_status_map[run_tag]}")
st.sidebar.markdown("---")
if st.sidebar.button("Refresh data"):
    load_data.clear()
    list_runs.clear()
    st.rerun()


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
ranked, workflow = load_data(run_tag)

# ---------------------------------------------------------------------------
# Header + summary metrics
# ---------------------------------------------------------------------------
st.title("PEMS — Picking Exception Management")
st.caption(f"Run: **{run_tag}**   ·   "
           f"Updated {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

col1, col2, col3, col4 = st.columns(4)
with col1:
    flagged = sum(1 for r in ranked if r["anomaly_events"] > 0)
    st.metric("Locations with anomalies", flagged)
with col2:
    pending = sum(1 for t in workflow if t["status"] == "pending")
    st.metric("Pending tasks", pending)
with col3:
    in_prog = sum(1 for t in workflow if t["status"] == "in_progress")
    st.metric("In progress", in_prog)
with col4:
    completed = sum(1 for t in workflow if t["status"] == "completed")
    st.metric("Completed", completed)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_ranking, tab_tasks = st.tabs(["Ranking", "Workflow"])


# --- Ranking tab -----------------------------------------------------------
with tab_ranking:
    st.subheader("Location Ranking")
    st.caption("Ranked by the combined score. The highest-risk locations are highlighted below.")

    visible = [r for r in ranked if r["anomaly_events"] > 0]
    top_4 = visible[:4]
    rest = visible[4:]

    # --- 1. The "Priority Cards" (Top 4 Locations) ---
    if top_4:
        st.markdown("### Top Priority Locations")
        cols = st.columns(min(len(top_4), 4))
        
        for idx, r in enumerate(top_4):
            loc_id = r["location_id"]
            score = round(r["proposed_score"], 2)
            
            # Determine card color/emoji based on score
            if score >= 0.7:
                color = "#ff4b4b" # Streamlit red
                badge = "🔴 HIGH RISK"
            elif score >= 0.4:
                color = "#ffa421" # Streamlit orange
                badge = "🟠 MEDIUM RISK"
            else:
                color = "#2ecc71" # Streamlit green
                badge = "🟢 LOW RISK"

            with cols[idx]:
                # Use Streamlit's container as a card
                with st.container(border=True):
                    st.markdown(f"#### **Location {loc_id}**")
                    st.markdown(f"<span style='color:{color}; font-weight:bold;'>{badge}</span>", unsafe_allow_html=True)
                    st.markdown(f"**Score:** {score}")
                    st.markdown(f"**Anomalies:** {r['anomaly_events']} *(from {r['distinct_pickers']} pickers)*")
                    
                    # Show if a task already exists
                    has_task = any(t["location_id"] == loc_id for t in workflow)
                    if has_task:
                        st.success("✅ Task already assigned")
                    else:
                        if st.button(f"Dispatch Shopper", key=f"dispatch_{loc_id}", use_container_width=True):
                            # Generate a single task inline
                            from pems.tasks import build_reason
                            c = psycopg2.connect(**DB_PARAMS)
                            try:
                                reason = build_reason(r)
                                priority = "high" if score >= 0.7 else "medium" if score >= 0.4 else "low"
                                with c.cursor() as cur:
                                    cur.execute("""
                                        INSERT INTO quality_tasks
                                            (location_id, task_reason, priority, status)
                                        VALUES (%s, %s, %s, 'pending');
                                    """, (loc_id, reason, priority))
                                c.commit()
                                load_data.clear()
                                st.rerun()
                            finally:
                                c.close()

    # --- 2. The Full Ranking Table ---
    st.divider()
    st.subheader("Full Ranking (All Flagged Locations)")
    
    table_rows = []
    for r in visible:
        priority = ("high" if r["proposed_score"] >= 0.7 else
                    "medium" if r["proposed_score"] >= 0.4 else "low")
        table_rows.append({
            "Rank": r["proposed_rank"],
            "Location": r["location_id"],
            "Score": round(r["proposed_score"], 3),
            "Priority": priority.upper(),
            "Anomalies": r["anomaly_events"],
            "Total events": r["total_events"],
            "Pickers": r["distinct_pickers"],
            "Dominant wrong product": r["dominant_wrong_product_id"],
            "Product consistency": round(r["P"], 2),
            "Recency (days)": round(r["T_days_window"], 1),
        })
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    # --- 3. Export ---
    st.divider()
    col_export, _ = st.columns([1, 2])
    with col_export:
        excel_buf = io.BytesIO()
        write_excel(run_tag, excel_buf)
        excel_buf.seek(0)
        st.download_button(
            label="📥 Download Excel Dispatch Sheet",
            data=excel_buf,
            file_name=f"PEMS_{run_tag}_dispatch.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True
        )

# --- Workflow tab ----------------------------------------------------------
with tab_tasks:
    st.subheader("Suggested tasks and outcomes")

    if not workflow:
        st.info("No tasks yet. Use the panel below to generate some from "
                "the current ranking.")
    else:
        conn = psycopg2.connect(**DB_PARAMS)
        try:
            for t in workflow:
                badge = {"pending": "🟡", "in_progress": "🔵",
                         "completed": "🟢", "cancelled": "⚪"}.get(
                    t["status"], "⚪")
                label = (
                    f"{badge} Task #{t['task_id']} — Location {t['location_id']} "
                    f"({t['location_code']}) — {t['priority'].upper()} — {t['status']}"
                )
                with st.expander(label):
                    st.markdown(f"**Reason:** {t['task_reason']}")
                    st.markdown(f"**Created:** {t['created_at']}")
                    if t["assigned_to"]:
                        st.markdown(f"**Assigned to:** {t['assigned_to']}")
                    if t["confirmed_cause"]:
                        st.markdown(f"**Confirmed cause:** `{t['confirmed_cause']}`")
                        st.markdown(f"**Notes:** {t['resolution_notes']}")
                    if t["resolved_at"]:
                        st.markdown(f"**Resolved:** {t['resolved_at']}")

                    # --- Pending: accept or reject ---
                    if t["status"] == "pending":
                        st.markdown("---")
                        c1, c2 = st.columns([1, 1])
                        with c1:
                            if st.button(f"✅ Accept and assign shopper",
                                         key=f"accept_{t['task_id']}"):
                                sid = accept_task(conn, t["task_id"])
                                st.success(f"Assigned to shopper {sid}. "
                                           f"Task moved to in_progress.")
                                load_data.clear()
                                st.rerun()
                        with c2:
                            reject_reason = st.text_input(
                                "Rejection reason (optional)",
                                key=f"rejreason_{t['task_id']}",
                                placeholder="e.g. already investigated last week",
                            )
                            if st.button(f"❌ Reject",
                                         key=f"reject_{t['task_id']}"):
                                reject_task(conn, t["task_id"],
                                            reject_reason or "no reason given")
                                st.warning("Task rejected.")
                                load_data.clear()
                                st.rerun()

                    # --- In progress: record outcome ---
                    elif t["status"] == "in_progress":
                        st.markdown("---")
                        st.markdown("**Record shopper outcome**")
                        cause = st.selectbox(
                            "Confirmed cause",
                            ["incorrect_product_placement", "damaged_barcode",
                             "mixed_products", "missing_stock",
                             "no_problem_found", "other"],
                            key=f"cause_{t['task_id']}",
                        )
                        notes = st.text_area(
                            "Shopper notes",
                            key=f"notes_{t['task_id']}",
                            placeholder="What did the shopper find at the shelf?",
                            height=80,
                        )
                        if st.button(f"📝 Complete task",
                                     key=f"complete_{t['task_id']}"):
                            ok = complete_task(conn, t["task_id"], run_tag,
                                               cause, notes or "(no notes)")
                            if ok:
                                st.success(f"Recorded: {cause}.")
                                load_data.clear()
                                st.rerun()
        finally:
            conn.close()

    # --- Generate tasks panel ---
    st.divider()
    st.subheader("Generate tasks from the current ranking")
    st.caption("Creates quality_tasks for the top 10% of ranked locations "
               "that do not already have a task.")

    existing = {t["location_id"] for t in workflow}
    visible = [r for r in ranked if r["anomaly_events"] > 0]
    top_n = max(1, int(round(len(ranked) * 0.10)))
    candidates = visible[:top_n]
    already_have = [r for r in candidates if r["location_id"] in existing]
    needs_task = [r for r in candidates if r["location_id"] not in existing]

    if already_have:
        st.info(
            f"Locations {[r['location_id'] for r in already_have]} already "
            f"have a task. They will be skipped."
        )

    if needs_task:
        if st.button(f"Generate {len(needs_task)} task(s)"):
            c = psycopg2.connect(**DB_PARAMS)
            try:
                inserted = []
                with c.cursor() as cur:
                    for row in needs_task:
                        reason = build_reason(row)
                        priority = (
                            "high" if row["proposed_score"] >= 0.7 else
                            "medium" if row["proposed_score"] >= 0.4 else "low"
                        )
                        cur.execute("""
                            INSERT INTO quality_tasks
                                (location_id, task_reason, priority, status)
                            VALUES (%s, %s, %s, 'pending')
                            RETURNING task_id;
                        """, (row["location_id"], reason, priority))
                        inserted.append((cur.fetchone()[0], row["location_id"]))
                c.commit()
                st.success(
                    f"Generated {len(inserted)} task(s) for locations "
                    f"{[loc for _, loc in inserted]}."
                )
                load_data.clear()
                st.rerun()
            finally:
                c.close()
    else:
        st.success("All top-ranked locations already have a task. "
                   "Nothing to generate.")

    # --- Reset ---
    with st.expander("Reset workflow for this run"):
        st.warning(
            "This deletes all tasks and outcomes for the current run so you "
            "can regenerate them from scratch. Detection data is untouched."
        )
        if st.button("Reset tasks and outcomes"):
            c = psycopg2.connect(**DB_PARAMS)
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        DELETE FROM quality_task_outcomes
                        WHERE task_id IN (
                            SELECT task_id FROM quality_tasks
                            WHERE location_id IN (
                                SELECT DISTINCT pe.location_id
                                FROM picking_events pe
                                JOIN simulation_event_labels el
                                  ON el.event_id = pe.event_id
                                WHERE el.run_tag = %s
                            )
                        );
                    """, (run_tag,))
                    cur.execute("""
                        DELETE FROM quality_tasks
                        WHERE location_id IN (
                            SELECT DISTINCT pe.location_id
                            FROM picking_events pe
                            JOIN simulation_event_labels el
                              ON el.event_id = pe.event_id
                            WHERE el.run_tag = %s
                        );
                    """, (run_tag,))
                c.commit()
                st.success(f"Reset tasks for run {run_tag}.")
                load_data.clear()
                st.rerun()
            finally:
                c.close()