"""Risk scoring: baselines + proposed combined score (blueprint §11.3)."""

from datetime import datetime, timezone

import psycopg2

from pems.config import DB_PARAMS
from pems.features import fetch_features


def normalise(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def score_locations(features, now=None):
    if now is None:
        now = datetime.now(timezone.utc)

    F = [r["anomaly_events"] for r in features]
    D = [r["distinct_pickers"] for r in features]
    P_raw = [
        (r["dominant_wrong_product_occurrences"] / r["anomaly_events"])
        if r["anomaly_events"] else 0.0
        for r in features
    ]
    T_raw = []
    for r in features:
        if r["last_anomaly_at"] is None:
            T_raw.append(0.0)
        else:
            days = (now - r["last_anomaly_at"]).total_seconds() / 86400.0
            T_raw.append(max(0.0, 30.0 - days))

    Fn, Dn, Pn, Tn = (normalise(x) for x in (F, D, P_raw, T_raw))

    out = []
    for i, r in enumerate(features):
        out.append({
            "location_id": r["location_id"],
            "total_events": r["total_events"],
            "anomaly_events": r["anomaly_events"],
            "distinct_pickers": r["distinct_pickers"],
            "distinct_wrong_products": r["distinct_wrong_products"],
            "dominant_wrong_product_id": r["dominant_wrong_product_id"],
            "dominant_wrong_product_occurrences": r["dominant_wrong_product_occurrences"],
            "F": F[i], "D": D[i], "P": P_raw[i], "T_days_window": T_raw[i],
            "Fn": Fn[i], "Dn": Dn[i], "Pn": Pn[i], "Tn": Tn[i],
            "baseline_1_frequency": F[i],
            "baseline_2_frequency_known_wrong": F[i],
            "baseline_3_pickers_ge_3": 1 if D[i] >= 3 else 0,
            "proposed_score": 0.25 * Fn[i] + 0.25 * Dn[i] + 0.25 * Pn[i] + 0.25 * Tn[i],
        })
    out.sort(key=lambda x: x["proposed_score"], reverse=True)
    for rank, row in enumerate(out, start=1):
        row["proposed_rank"] = rank
    return out


def main():
    conn = psycopg2.connect(**DB_PARAMS)
    try:
        feats = fetch_features("SIM001", conn)
        scored = score_locations(feats)
        print(f"{'rank':>4} {'loc':>4} {'F':>4} {'D':>4} {'P':>5} {'score':>7}")
        for row in scored[:12]:
            print(f"{row['proposed_rank']:>4} {row['location_id']:>4} "
                  f"{row['F']:>4} {row['D']:>4} {row['P']:>5.2f} "
                  f"{row['proposed_score']:>7.3f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()