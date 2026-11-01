"""Suggested quality-task generation (top 10% by proposed score)."""

import psycopg2

from pems.config import DB_PARAMS
from pems.features import fetch_features
from pems.scoring import score_locations


def build_reason(row):
    parts = [f"{row['anomaly_events']} product-mismatch events",
             f"{row['distinct_pickers']} different pickers affected"]
    if row["dominant_wrong_product_id"] is not None:
        parts.append(
            f"same unexpected product ({row['dominant_wrong_product_id']}) "
            f"rejected {row['dominant_wrong_product_occurrences']} times"
        )
    parts.append(f"observed at {row['total_events']} total picks this location")
    return "; ".join(parts)


def generate_tasks(run_tag, top_fraction=0.10, conn=None, dry_run=False):
    own_conn = conn is None
    if own_conn:
        conn = psycopg2.connect(**DB_PARAMS)
    try:
        scored = score_locations(fetch_features(run_tag, conn))
        n_tasks = max(1, int(round(len(scored) * top_fraction)))
        selected = scored[:n_tasks]

        created = []
        with conn.cursor() as cur:
            for row in selected:
                if row["anomaly_events"] == 0:
                    continue
                reason = build_reason(row)
                priority = ("high" if row["proposed_score"] >= 0.7 else
                            "medium" if row["proposed_score"] >= 0.4 else "low")
                if dry_run:
                    created.append((row["location_id"], priority, reason))
                    continue
                cur.execute("""
                    INSERT INTO quality_tasks
                        (location_id, task_reason, priority, status)
                    VALUES (%s, %s, %s, 'pending')
                    RETURNING task_id;
                """, (row["location_id"], reason, priority))
                created.append((cur.fetchone()[0], row["location_id"], reason))
            if not dry_run:
                conn.commit()
        return created
    finally:
        if own_conn:
            conn.close()


def main():
    tasks = generate_tasks("SIM001", dry_run=True)
    print(f"Would generate {len(tasks)} tasks:")
    for row in tasks:
        print(f"  {row}")


if __name__ == "__main__":
    main()