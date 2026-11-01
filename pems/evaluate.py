"""Evaluate Module A ranking against hidden ground truth (blueprint §11)."""

import psycopg2

from pems.config import DB_PARAMS
from pems.features import fetch_features
from pems.scoring import score_locations


def ground_truth(conn, run_tag):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT pe.location_id
            FROM simulation_event_labels l
            JOIN picking_events pe USING (event_id)
            WHERE l.run_tag = %s AND l.is_hotspot_location = TRUE;
        """, (run_tag,))
        return {r[0] for r in cur.fetchall()}


def evaluate(scored, truth, all_locations, k_values=(3, 4, 5, 8, 10)):
    negatives = all_locations - truth
    results = []
    for method in ("baseline_1_frequency",
                   "baseline_2_frequency_known_wrong",
                   "baseline_3_pickers_ge_3",
                   "proposed_score"):
        for k in k_values:
            if method == "baseline_3_pickers_ge_3":
                if k != k_values[0]:
                    continue
                selected = {r["location_id"] for r in scored if r[method] == 1}
                k_repr = ">=3 pickers"
            else:
                ordered = sorted(scored, key=lambda r: r[method], reverse=True)
                selected = {r["location_id"] for r in ordered[:k]}
                k_repr = k

            tp = len(selected & truth)
            fp = len(selected - truth)
            fn = len(truth - selected)
            precision = tp / (tp + fp) if (tp + fp) else 0.0
            recall = tp / (tp + fn) if (tp + fn) else 0.0
            fpr = fp / len(negatives) if negatives else 0.0
            results.append({
                "method": method, "k": k_repr, "selected": len(selected),
                "tp": tp, "fp": fp, "fn": fn,
                "precision": precision, "recall": recall, "fpr": fpr,
            })
    return results


def main():
    import sys
    run_tag = sys.argv[1] if len(sys.argv) > 1 else "SIM001"
    conn = psycopg2.connect(**DB_PARAMS)
    try:
        feats = fetch_features(run_tag, conn)
        scored = score_locations(feats)
        truth = ground_truth(conn, run_tag)
        all_locations = {r["location_id"] for r in feats}

        print(f"Ground-truth hotspots: {sorted(truth)}")
        print(f"Total locations: {len(all_locations)}\n")

        results = evaluate(scored, truth, all_locations)
        print(f"{'method':<36} {'k':>12} {'sel':>4} {'tp':>3} {'fp':>3} "
              f"{'prec':>6} {'rec':>6} {'fpr':>6}")
        for r in results:
            print(f"{r['method']:<36} {str(r['k']):>12} {r['selected']:>4} "
                  f"{r['tp']:>3} {r['fp']:>3} {r['precision']:>6.3f} "
                  f"{r['recall']:>6.3f} {r['fpr']:>6.3f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()