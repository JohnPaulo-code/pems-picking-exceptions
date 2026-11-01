"""
Workflow loop: captain review -> shopper assignment -> investigation -> outcome.

Downstream of detection. Reads quality_tasks (from pems/tasks.py),
writes quality_task_outcomes.

Modes:
  oracle    — outcome derived from simulation_event_labels (evaluation mode)
  realistic — outcome inferred from observables only (shopper's view)

Two built-in fixes vs the earlier draft:
  1. resolved_at is anchored inside the simulated timeline (2 days before the
     location's last event) so recurrence is measurable, not vacuously zero.
  2. Realistic mode adds a recency check: if the last anomaly at a location is
     more than 3 days old, the shopper concludes "no_problem_found" — which is
     what they would actually find if they visited today.
"""

import argparse
import random
from datetime import datetime, timedelta, timezone

import psycopg2

from pems.config import DB_PARAMS


# ===========================================================================
# Captain review
# ===========================================================================
def captain_review(conn, run_tag, rng=None):
    if rng is None:
        rng = random.Random(7)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT task_id, location_id, priority
            FROM quality_tasks
            WHERE status = 'pending'
              AND location_id IN (
                  SELECT DISTINCT pe.location_id
                  FROM picking_events pe
                  JOIN simulation_event_labels l ON l.event_id = pe.event_id
                  WHERE l.run_tag = %s
              )
            ORDER BY
              CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
              task_id;
        """, (run_tag,))
        tasks = cur.fetchall()

        accepted, rejected = [], []
        for task_id, loc_id, priority in tasks:
            p_accept = {"high": 0.95, "medium": 0.75, "low": 0.50}[priority]
            if rng.random() < p_accept:
                cur.execute("""
                    UPDATE quality_tasks
                    SET status = 'in_progress',
                        assigned_shopper_id = (
                            SELECT employee_id FROM employees
                            WHERE employee_role = 'quality_shopper'
                            ORDER BY random() LIMIT 1
                        )
                    WHERE task_id = %s
                    RETURNING task_id, assigned_shopper_id;
                """, (task_id,))
                tid, sid = cur.fetchone()
                accepted.append({"task_id": tid, "location_id": loc_id,
                                 "assigned_to": sid})
            else:
                cur.execute("""
                    UPDATE quality_tasks SET status = 'cancelled'
                    WHERE task_id = %s;
                """, (task_id,))
                rejected.append({"task_id": task_id, "location_id": loc_id})
        conn.commit()
        return accepted, rejected


# ===========================================================================
# Shopper investigation
# ===========================================================================
def infer_outcome_oracle(cur, run_tag, location_id):
    cur.execute("""
        SELECT scenario, COUNT(*)
        FROM simulation_event_labels l
        JOIN picking_events pe ON pe.event_id = l.event_id
        WHERE l.run_tag = %s AND pe.location_id = %s AND l.is_anomaly = TRUE
        GROUP BY scenario ORDER BY COUNT(*) DESC;
    """, (run_tag, location_id))
    rows = cur.fetchall()
    if not rows:
        return "no_problem_found", "No anomaly events associated with location."

    scenario = rows[0][0]
    mapping = {
        "incorrect_shelf_product": "incorrect_product_placement",
        "damaged_barcode":         "damaged_barcode",
        "missing_stock":           "missing_stock",
        "mixed_physical_products": "mixed_products",
    }
    cause = mapping.get(scenario, "other")
    total = sum(n for _, n in rows)
    notes = (f"Observed {total} anomaly events; dominant pattern: {scenario}. "
             f"Physical inspection confirmed {cause}.")
    return cause, notes


def infer_outcome_realistic(cur, run_tag, location_id):
    """
    Realistic mode: infer from observables only.

    Logic:
      - No anomalies at all                          -> no_problem_found
      - Last anomaly > 3 days ago (relative to the   -> no_problem_found
        location's most recent event)                   (shelf is fine now)
      - Dominant product consistency >= 0.7          -> incorrect_product_placement
      - Otherwise                                    -> mixed_products
    """
    cur.execute("""
        WITH run_events AS (
            SELECT pe.event_id, pe.event_time
            FROM picking_events pe
            JOIN simulation_event_labels l ON l.event_id = pe.event_id
            WHERE l.run_tag = %s AND pe.location_id = %s
        ),
        anomaly_events AS (
            SELECT DISTINCT e.event_id, e.event_time
            FROM run_events e
            WHERE EXISTS (
                SELECT 1 FROM scan_attempts sa
                WHERE sa.event_id = e.event_id
                  AND sa.is_accepted = FALSE
                  AND sa.scanned_product_id IS NOT NULL
            )
        ),
        wrong_products AS (
            SELECT sa.scanned_product_id, COUNT(*) AS n
            FROM scan_attempts sa
            JOIN anomaly_events ae ON ae.event_id = sa.event_id
            WHERE sa.is_accepted = FALSE AND sa.scanned_product_id IS NOT NULL
            GROUP BY sa.scanned_product_id
        )
        SELECT
            (SELECT COUNT(*) FROM anomaly_events)              AS n_anomalies,
            COALESCE((SELECT MAX(n) FROM wrong_products), 0)   AS top_product_n,
            (SELECT MAX(event_time) FROM anomaly_events)       AS last_anomaly_at,
            (SELECT MAX(event_time) FROM run_events)           AS last_event_at;
    """, (run_tag, location_id))
    n_anomalies, top_n, last_anomaly_at, last_event_at = cur.fetchone()

    if n_anomalies == 0:
        return "no_problem_found", "No observable mismatches at this location."

    # Recency check: if the last anomaly is >3 days before the last event,
    # the shopper visiting today finds a clean shelf.
    if last_anomaly_at and last_event_at:
        staleness = (last_event_at - last_anomaly_at).total_seconds() / 86400.0
        if staleness > 3.0:
            return "no_problem_found", (
                f"{n_anomalies} historical mismatches but none in the last "
                f"{staleness:.1f} days. Shelf is currently clean."
            )

    consistency = top_n / n_anomalies
    if consistency >= 0.7:
        return "incorrect_product_placement", (
            f"Dominant unexpected product accounts for {top_n}/{n_anomalies} "
            f"mismatches ({consistency:.0%} consistency). Shelf confirmed."
        )
    return "mixed_products", (
        f"{n_anomalies} mismatches but no dominant product "
        f"(top product only {top_n}/{n_anomalies}). Shelf may be mixed."
    )


def _resolve_at(cur, run_tag, location_id):
    """
    Anchor resolution inside the simulated timeline: 2 days before the last
    event at this location. Makes anomalies-after-resolution non-vacuous.
    """
    cur.execute("""
        SELECT MAX(pe.event_time) - INTERVAL '2 days'
        FROM picking_events pe
        JOIN simulation_event_labels l ON l.event_id = pe.event_id
        WHERE l.run_tag = %s AND pe.location_id = %s;
    """, (run_tag, location_id))
    row = cur.fetchone()
    if row and row[0] is not None:
        return row[0]
    return datetime.now(timezone.utc)


def conduct_investigation(conn, run_tag, accepted, mode="oracle"):
    completed = []
    with conn.cursor() as cur:
        for item in accepted:
            loc = item["location_id"]
            if mode == "oracle":
                cause, notes = infer_outcome_oracle(cur, run_tag, loc)
            else:
                cause, notes = infer_outcome_realistic(cur, run_tag, loc)

            resolved_at = _resolve_at(cur, run_tag, loc)

            cur.execute("""
                INSERT INTO quality_task_outcomes
                    (task_id, confirmed_cause, resolution_notes, resolved_at)
                VALUES (%s, %s, %s, %s)
                RETURNING outcome_id;
            """, (item["task_id"], cause, notes, resolved_at))
            outcome_id = cur.fetchone()[0]

            cur.execute("""
                UPDATE quality_tasks SET status = 'completed'
                WHERE task_id = %s;
            """, (item["task_id"],))

            completed.append({
                "task_id": item["task_id"],
                "location_id": loc,
                "confirmed_cause": cause,
                "outcome_id": outcome_id,
                "resolved_at": resolved_at,
            })
        conn.commit()
    return completed


# ===========================================================================
# Recurrence
# ===========================================================================
def evaluate_recurrence(conn, run_tag, completed):
    results = []
    with conn.cursor() as cur:
        for item in completed:
            cur.execute("""
                SELECT COUNT(*)
                FROM simulation_event_labels l
                JOIN picking_events pe ON pe.event_id = l.event_id
                WHERE l.run_tag = %s
                  AND pe.location_id = %s
                  AND l.is_anomaly = TRUE
                  AND pe.event_time > %s;
            """, (run_tag, item["location_id"], item["resolved_at"]))
            post_count = cur.fetchone()[0]
            results.append({
                "location_id": item["location_id"],
                "confirmed_cause": item["confirmed_cause"],
                "anomalies_after_resolution": post_count,
            })
    return results


# ===========================================================================
# Report
# ===========================================================================
def report(conn, run_tag, accepted, rejected, completed, recurrence):
    print("\n=== WORKFLOW LOOP REPORT ===")
    print(f"Run tag:      {run_tag}")
    print(f"Reviewed:     {len(accepted) + len(rejected)}")
    print(f"Accepted:     {len(accepted)}")
    print(f"Rejected:     {len(rejected)}")
    print(f"Completed:    {len(completed)}")

    print("\n--- Confirmed causes ---")
    cause_counts = {}
    for c in completed:
        cause_counts[c["confirmed_cause"]] = cause_counts.get(c["confirmed_cause"], 0) + 1
    for cause, n in sorted(cause_counts.items(), key=lambda x: -x[1]):
        print(f"  {cause:<30} {n}")

    print("\n--- Per-task outcomes ---")
    print(f"{'loc':>4} {'confirmed_cause':<30} {'anomalies_after':>15}")
    for r in recurrence:
        print(f"{r['location_id']:>4} {r['confirmed_cause']:<30} "
              f"{r['anomalies_after_resolution']:>15}")

    print("\n--- §11.2 investigation metrics ---")
    total = len(accepted) + len(rejected)
    if total:
        print(f"  Task acceptance rate:  {len(accepted)/total:.1%}")
        print(f"  Investigation rate:    {len(completed)/max(len(accepted),1):.1%}")
    detected = sum(1 for c in completed
                   if c["confirmed_cause"] == "incorrect_product_placement")
    if completed:
        print(f"  Confirmed as detected: {detected}/{len(completed)} "
              f"({detected/len(completed):.1%})")


# ===========================================================================
# Entry point
# ===========================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_tag", nargs="?", default="SIM003")
    parser.add_argument("--mode", choices=["oracle", "realistic"], default="oracle")
    parser.add_argument("--reset", action="store_true",
                        help="Delete prior tasks/outcomes for this run before starting")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB_PARAMS)
    try:
        if args.reset:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM quality_task_outcomes
                    WHERE task_id IN (
                        SELECT task_id FROM quality_tasks
                        WHERE location_id IN (
                            SELECT DISTINCT pe.location_id
                            FROM picking_events pe
                            JOIN simulation_event_labels l ON l.event_id = pe.event_id
                            WHERE l.run_tag = %s
                        )
                    );
                """, (args.run_tag,))
                cur.execute("""
                    DELETE FROM quality_tasks
                    WHERE location_id IN (
                        SELECT DISTINCT pe.location_id
                        FROM picking_events pe
                        JOIN simulation_event_labels l ON l.event_id = pe.event_id
                        WHERE l.run_tag = %s
                    );
                """, (args.run_tag,))
                conn.commit()
            print("Reset: cleared prior tasks/outcomes for this run.")

        print(f"Mode: {args.mode}")
        print("Captain review...")
        accepted, rejected = captain_review(conn, args.run_tag)
        print(f"  accepted {len(accepted)}, rejected {len(rejected)}")

        print("Conducting investigations...")
        completed = conduct_investigation(conn, args.run_tag, accepted, mode=args.mode)
        print(f"  completed {len(completed)}")

        print("Evaluating recurrence...")
        recurrence = evaluate_recurrence(conn, args.run_tag, completed)

        report(conn, args.run_tag, accepted, rejected, completed, recurrence)
    finally:
        conn.close()


if __name__ == "__main__":
    main()