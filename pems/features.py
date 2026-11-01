"""
Per-location features from operational data only.

Never reads simulation_event_labels for feature values (only the run-scoping
JOIN, which filters events by run tag — this is not label leakage because the
features themselves are computed from picking_events and scan_attempts).

Features (blueprint §6.2):
  F  anomaly frequency     — count of events with a rejected known-wrong scan
  D  picker diversity      — distinct pickers who hit such events
  P  product consistency   — dominant wrong product share of anomaly events
  T  recency               — days since last anomaly (inverted in scoring)
  N  activity              — total picking events at this location
"""

import psycopg2

from pems.config import DB_PARAMS


FEATURES_SQL = """
WITH run_events AS (
    SELECT pe.event_id, pe.location_id, pe.picker_id, pe.event_time
    FROM picking_events pe
    JOIN simulation_event_labels l ON l.event_id = pe.event_id
    WHERE l.run_tag = %(run_tag)s
),
anomaly_events AS (
    SELECT DISTINCT e.event_id, e.location_id, e.picker_id, e.event_time
    FROM run_events e
    WHERE EXISTS (
        SELECT 1 FROM scan_attempts sa
        WHERE sa.event_id = e.event_id
          AND sa.is_accepted = FALSE
          AND sa.scanned_product_id IS NOT NULL
    )
),
event_wrong_product AS (
    SELECT DISTINCT ON (sa.event_id)
           sa.event_id, sa.scanned_product_id AS wrong_product_id
    FROM scan_attempts sa
    JOIN anomaly_events ae ON ae.event_id = sa.event_id
    WHERE sa.is_accepted = FALSE AND sa.scanned_product_id IS NOT NULL
    ORDER BY sa.event_id, sa.scan_time ASC
),
per_location AS (
    SELECT e.location_id,
           COUNT(*)          AS total_events,
           MAX(e.event_time) AS last_event_at
    FROM run_events e
    GROUP BY e.location_id
),
per_location_anomalies AS (
    SELECT ae.location_id,
           COUNT(*)                          AS anomaly_events,
           COUNT(DISTINCT ae.picker_id)      AS distinct_pickers,
           MAX(ae.event_time)                AS last_anomaly_at,
           COUNT(DISTINCT ewp.wrong_product_id) AS distinct_wrong_products
    FROM anomaly_events ae
    LEFT JOIN event_wrong_product ewp ON ewp.event_id = ae.event_id
    GROUP BY ae.location_id
),
dominant_wrong_product AS (
    SELECT location_id, wrong_product_id, n
    FROM (
        SELECT ae.location_id,
               ewp.wrong_product_id,
               COUNT(*) AS n,
               ROW_NUMBER() OVER (
                   PARTITION BY ae.location_id
                   ORDER BY COUNT(*) DESC, ewp.wrong_product_id
               ) AS rn
        FROM anomaly_events ae
        JOIN event_wrong_product ewp ON ewp.event_id = ae.event_id
        GROUP BY ae.location_id, ewp.wrong_product_id
    ) x
    WHERE rn = 1
)
SELECT
    pl.location_id,
    pl.total_events,
    COALESCE(pla.anomaly_events, 0)                   AS anomaly_events,
    COALESCE(pla.distinct_pickers, 0)                 AS distinct_pickers,
    COALESCE(pla.distinct_wrong_products, 0)          AS distinct_wrong_products,
    COALESCE(dwp.wrong_product_id, NULL)              AS dominant_wrong_product_id,
    COALESCE(dwp.n, 0)                                AS dominant_wrong_product_occurrences,
    pla.last_anomaly_at,
    pl.last_event_at
FROM per_location pl
LEFT JOIN per_location_anomalies pla ON pla.location_id = pl.location_id
LEFT JOIN dominant_wrong_product dwp ON dwp.location_id = pl.location_id
ORDER BY pl.location_id;
"""


def fetch_features(run_tag, conn):
    with conn.cursor() as cur:
        cur.execute(FEATURES_SQL, {"run_tag": run_tag})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def main():
    conn = psycopg2.connect(**DB_PARAMS)
    try:
        rows = fetch_features("SIM001", conn)
        print(f"{'loc':>4} {'events':>7} {'anoms':>6} {'pickers':>7} "
              f"{'uniq_wrong':>10} {'dom_wrong':>10} {'dom_n':>6}")
        for r in rows:
            if r["anomaly_events"] > 0:
                print(f"{r['location_id']:>4} {r['total_events']:>7} "
                      f"{r['anomaly_events']:>6} {r['distinct_pickers']:>7} "
                      f"{r['distinct_wrong_products']:>10} "
                      f"{str(r['dominant_wrong_product_id']):>10} "
                      f"{r['dominant_wrong_product_occurrences']:>6}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()