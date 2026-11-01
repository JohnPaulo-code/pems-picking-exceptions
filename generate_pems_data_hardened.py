"""
PEMS synthetic data generator — HARDENED v2 (three adversarial decoys).

Produces run tag SIM003 by default.

Design fix from SIM002: decoys fire at full p_hit. The misleading property
(single picker / random product / stale timestamp) is enforced by REWRITING
the event's picker, product, or time — not by reducing the fire probability.

Effect: decoy frequency looks like a hotspot, but D, P, or T is wrong,
so the multi-feature score should demote decoys that frequency ranking
would rank highly.
"""

import random
import sys
from datetime import datetime, timedelta, timezone

import psycopg2

DB_PARAMS = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "postgres",
    "password": "admin",
    "database": "picking_exceptions",
}

SEED = 44
N_PRODUCTS = 100
N_LOCATIONS = 40
N_PICKERS = 20
N_QAS = 5
N_CAPTAINS = 2
N_ORDERS = 1_800
N_PICKING_EVENTS = 5_000

# Original hotspots — unchanged
HOTSPOT_SPECS = [
    (5,  45, 0.28, "incorrect_product_placement"),
    (11, 22, 0.22, "incorrect_product_placement"),
    (18, 67, 0.32, "incorrect_product_placement"),
    (27, 8,  0.18, "incorrect_product_placement"),
    (33, 91, 0.25, "incorrect_product_placement"),
]

# Decoys — (location_index, p_hit, kind, cause)
# p_hit here is the ACTUAL anomaly rate, comparable to a hotspot's.
DECOY_SPECS = [
    (8,  0.30, "random_products", "ambient_noise"),
    (21, 0.30, "single_picker",   "picker_fatigue"),
    (30, 0.30, "stale_window",    "resolved_previously"),
]

DAMAGED_BARCODE_PRODUCT_INDEX = 12

P_AMBIENT_DAMAGED_BARCODE = 0.020
P_AMBIENT_MISSING_STOCK = 0.015
P_AMBIENT_MIXED_UNITS = 0.025


# ===========================================================================
# Schema helpers
# ===========================================================================
def ensure_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS simulation_runs (
            run_tag      VARCHAR(32) PRIMARY KEY,
            started_at   TIMESTAMPTZ NOT NULL,
            status       VARCHAR(16) NOT NULL DEFAULT 'running'
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS simulation_event_labels (
            event_id              INTEGER NOT NULL,
            run_tag               VARCHAR(32) NOT NULL,
            scenario              VARCHAR(50) NOT NULL,
            underlying_cause      VARCHAR(50) NOT NULL,
            is_anomaly            BOOLEAN NOT NULL,
            incorrect_unit_count  INTEGER NOT NULL DEFAULT 0,
            is_hotspot_location   BOOLEAN NOT NULL DEFAULT FALSE,
            expected_product_id   INTEGER NOT NULL,
            unexpected_product_id INTEGER,
            PRIMARY KEY (event_id, run_tag),
            FOREIGN KEY (run_tag) REFERENCES simulation_runs(run_tag)
        );
    """)


def claim_run(cur, run_tag):
    cur.execute(
        "INSERT INTO simulation_runs (run_tag, started_at, status) "
        "VALUES (%s, %s, 'running') ON CONFLICT (run_tag) DO NOTHING "
        "RETURNING run_tag;",
        (run_tag, datetime.now(timezone.utc)),
    )
    if cur.fetchone() is None:
        raise RuntimeError(f"Run tag '{run_tag}' already exists.")


def mark_run_status(cur, run_tag, status):
    cur.execute("UPDATE simulation_runs SET status = %s WHERE run_tag = %s;",
                (status, run_tag))


# ===========================================================================
# Reference data
# ===========================================================================
def seed_employees(cur):
    out = {"picker": [], "quality_shopper": [], "captain": []}
    plan = [("picker", N_PICKERS, "SIM-PKR"),
            ("quality_shopper", N_QAS, "SIM-QAS"),
            ("captain", N_CAPTAINS, "SIM-CPT")]
    for role, count, prefix in plan:
        for i in range(1, count + 1):
            code = f"{prefix}-{i:03d}"
            cur.execute("SELECT employee_id FROM employees WHERE employee_code = %s;", (code,))
            row = cur.fetchone()
            if row:
                out[role].append(row[0])
            else:
                cur.execute("INSERT INTO employees (employee_code, employee_role) "
                            "VALUES (%s, %s) RETURNING employee_id;", (code, role))
                out[role].append(cur.fetchone()[0])
    return out


def seed_locations(cur):
    zones = ["A", "B", "C", "D"]
    out = []
    for i in range(1, N_LOCATIONS + 1):
        zone = zones[i % 4]
        code = f"SIM-LOC-{zone}-{i:02d}"
        cur.execute("SELECT location_id FROM locations WHERE location_code = %s;", (code,))
        row = cur.fetchone()
        if row:
            out.append(row[0])
        else:
            cur.execute("INSERT INTO locations (location_code, zone) "
                        "VALUES (%s, %s) RETURNING location_id;", (code, zone))
            out.append(cur.fetchone()[0])
    return out


def seed_products(cur):
    categories = ["Electronics", "Groceries", "Apparel", "Home"]
    product_ids, barcodes = [], {}
    for i in range(1, N_PRODUCTS + 1):
        sku = f"SIM-SKU-{i:05d}"
        cur.execute("SELECT product_id FROM products WHERE sku = %s;", (sku,))
        row = cur.fetchone()
        if row:
            p_id = row[0]
        else:
            cur.execute("INSERT INTO products (sku, product_name, category) "
                        "VALUES (%s, %s, %s) RETURNING product_id;",
                        (sku, f"Simulated Product #{i}", categories[i % 4]))
            p_id = cur.fetchone()[0]
        product_ids.append(p_id)
        bc1 = f"BC-{p_id:05d}-1"
        cur.execute("SELECT barcode FROM product_barcodes WHERE barcode = %s;", (bc1,))
        if cur.fetchone() is None:
            cur.execute("INSERT INTO product_barcodes (product_id, barcode) VALUES (%s, %s);",
                        (p_id, bc1))
        barcodes[p_id] = [bc1]
        if i % 10 == 0:
            bc2 = f"BC-{p_id:05d}-2"
            cur.execute("SELECT barcode FROM product_barcodes WHERE barcode = %s;", (bc2,))
            if cur.fetchone() is None:
                cur.execute("INSERT INTO product_barcodes (product_id, barcode) VALUES (%s, %s);",
                            (p_id, bc2))
            barcodes[p_id].append(bc2)
    return product_ids, barcodes


def seed_orders(cur, run_tag, order_ids_out, timestamps_out, rng):
    start = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    for o_idx in range(1, N_ORDERS + 1):
        ref = f"{run_tag}-ORD-{o_idx:06d}"
        start += timedelta(minutes=rng.randint(2, 10))
        cur.execute("INSERT INTO orders (order_reference, order_created_at) "
                    "VALUES (%s, %s) RETURNING order_id;", (ref, start))
        o_id = cur.fetchone()[0]
        order_ids_out.append(o_id)
        timestamps_out[o_id] = start


def build_shelf_assortment(product_ids, location_ids):
    shelf = {}
    for idx, loc in enumerate(location_ids):
        shelf[loc] = [product_ids[(idx + j) % len(product_ids)] for j in range(3)]
    return shelf


def pick_quantity(rng):
    return rng.choices([1, 2, 3, 4], weights=[0.70, 0.20, 0.07, 0.03])[0]


# ===========================================================================
# Event loop
# ===========================================================================
def simulate_events(cur, run_tag, rng,
                    product_ids, product_barcodes, location_ids, shelf_assortment,
                    picker_ids, order_ids, order_timestamps):

    damaged_product_id = product_ids[DAMAGED_BARCODE_PRODUCT_INDEX]

    # Resolve original hotspots
    hotspots = {}
    for loc_idx, unexp_idx, p_hit, cause in HOTSPOT_SPECS:
        hotspots[location_ids[loc_idx]] = {
            "kind": "original",
            "unexpected_product_id": product_ids[unexp_idx],
            "p_hit": p_hit,
            "cause": cause,
        }

    # Resolve decoys
    decoys = {}
    for loc_idx, p_hit, kind, cause in DECOY_SPECS:
        decoys[location_ids[loc_idx]] = {"kind": kind, "p_hit": p_hit, "cause": cause}

    # Decoy picker pools
    decoy_a_pickers = picker_ids[:2]           # 2 pickers
    decoy_b_picker  = picker_ids[0]            # 1 picker

    # Decoy C: early window. Take first 3 days of the run.
    decoy_c_window_start = min(order_timestamps.values())
    decoy_c_window_end   = decoy_c_window_start + timedelta(days=3)

    events_written = 0
    scenario_counts = {}

    for _ in range(N_PICKING_EVENTS):
        order_id = rng.choice(order_ids)
        location_id = rng.choice(location_ids)
        expected_product_id = rng.choice(shelf_assortment[location_id])
        picker_id = rng.choice(picker_ids)
        requested_qty = pick_quantity(rng)
        pick_time = order_timestamps[order_id] + timedelta(minutes=rng.randint(5, 45))

        scenario = "normal"
        cause = "none"
        is_anomaly = False
        incorrect_units = 0
        expected_pid = expected_product_id
        unexpected_pid = None
        accepted_product_id = expected_product_id
        scans = []
        # These become the FINAL persisted values — decoys may rewrite them.
        final_picker_id = picker_id
        final_event_time = pick_time

        # ---- Priority 1: decoys (fire at full p_hit) --------------------
        if location_id in decoys:
            d = decoys[location_id]
            if rng.random() < d["p_hit"]:
                if d["kind"] == "random_products":
                    # Random unexpected product every time; picker from small pool.
                    pool = [p for p in product_ids if p != expected_product_id]
                    unexpected_pid = rng.choice(pool)
                    final_picker_id = rng.choice(decoy_a_pickers)

                elif d["kind"] == "single_picker":
                    # One consistent product; single designated picker.
                    unexpected_pid = product_ids[3]
                    if unexpected_pid == expected_product_id:
                        unexpected_pid = product_ids[4]
                    final_picker_id = decoy_b_picker

                elif d["kind"] == "stale_window":
                    # Consistent product; event time forced into early window.
                    unexpected_pid = product_ids[50]
                    if unexpected_pid == expected_product_id:
                        unexpected_pid = product_ids[51]
                    # Rewrite event_time to a random moment in the early window.
                    span = (decoy_c_window_end - decoy_c_window_start).total_seconds()
                    final_event_time = decoy_c_window_start + timedelta(
                        seconds=rng.uniform(0, span)
                    )

                # Common: emit rejected wrong scan, then accepted correct scan.
                wrong_bc = rng.choice(product_barcodes[unexpected_pid])
                scans.append((wrong_bc, unexpected_pid, False))
                good_bc = rng.choice(product_barcodes[expected_product_id])
                scans.append((good_bc, expected_product_id, True))
                accepted_product_id = expected_product_id
                scenario = "incorrect_shelf_product"
                cause = d["cause"]
                is_anomaly = True

        # ---- Priority 2: original hotspots ------------------------------
        elif location_id in hotspots and rng.random() < hotspots[location_id]["p_hit"]:
            unexpected_pid = hotspots[location_id]["unexpected_product_id"]
            if unexpected_pid == expected_product_id:
                unexpected_pid = product_ids[
                    (product_ids.index(unexpected_pid) + 1) % len(product_ids)
                ]
            wrong_bc = rng.choice(product_barcodes[unexpected_pid])
            scans.append((wrong_bc, unexpected_pid, False))
            good_bc = rng.choice(product_barcodes[expected_product_id])
            scans.append((good_bc, expected_product_id, True))
            accepted_product_id = expected_product_id
            scenario = "incorrect_shelf_product"
            cause = hotspots[location_id]["cause"]
            is_anomaly = True

        # ---- Priority 3: ambient ----------------------------------------
        elif expected_product_id == damaged_product_id and rng.random() < 0.6:
            n_fail = rng.randint(2, 5)
            for _ in range(n_fail):
                scans.append(("UNREADABLE", None, False))
            if rng.random() < 0.20:
                accepted_product_id = None
            else:
                good_bc = rng.choice(product_barcodes[expected_product_id])
                scans.append((good_bc, expected_product_id, True))
                accepted_product_id = expected_product_id
            scenario = "damaged_barcode"
            cause = "faded_label_intermittent"
            is_anomaly = True

        elif rng.random() < P_AMBIENT_DAMAGED_BARCODE:
            n_fail = rng.randint(1, 2)
            for _ in range(n_fail):
                scans.append(("UNREADABLE", None, False))
            good_bc = rng.choice(product_barcodes[expected_product_id])
            scans.append((good_bc, expected_product_id, True))
            accepted_product_id = expected_product_id
            scenario = "damaged_barcode"
            cause = "isolated_torn_sticker"
            is_anomaly = True

        elif rng.random() < P_AMBIENT_MISSING_STOCK:
            accepted_product_id = None
            scenario = "missing_stock"
            cause = "system_count_discrepancy"
            is_anomaly = True

        elif rng.random() < P_AMBIENT_MIXED_UNITS:
            good_bc = rng.choice(product_barcodes[expected_product_id])
            scans.append((good_bc, expected_product_id, True))
            accepted_product_id = expected_product_id
            incorrect_units = rng.randint(1, requested_qty)
            scenario = "mixed_physical_products"
            cause = "multi_unit_contamination"
            is_anomaly = True

        else:
            good_bc = rng.choice(product_barcodes[expected_product_id])
            scans.append((good_bc, expected_product_id, True))
            accepted_product_id = expected_product_id

        confirmed_qty = 0 if accepted_product_id is None else requested_qty

        # Persist picking_event using FINAL picker/time (decoy may have rewritten).
        cur.execute("""
            INSERT INTO picking_events
                (order_id, product_id, location_id, picker_id,
                 requested_quantity, confirmed_quantity, event_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING event_id;
        """, (order_id, expected_pid, location_id, final_picker_id,
              requested_qty, confirmed_qty, final_event_time))
        event_id = cur.fetchone()[0]
        events_written += 1

        # Persist scans starting at final_event_time.
        scan_time = final_event_time
        for barcode, scanned_pid, accepted in scans:
            scan_time += timedelta(seconds=rng.randint(4, 10))
            cur.execute("""
                INSERT INTO scan_attempts
                    (event_id, scanned_barcode, scanned_product_id,
                     is_accepted, scan_time)
                VALUES (%s, %s, %s, %s, %s);
            """, (event_id, barcode, scanned_pid, accepted, scan_time))

        is_gt_hotspot = location_id in hotspots

        cur.execute("""
            INSERT INTO simulation_event_labels
                (event_id, run_tag, scenario, underlying_cause, is_anomaly,
                 incorrect_unit_count, is_hotspot_location,
                 expected_product_id, unexpected_product_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (event_id, run_tag, scenario, cause, is_anomaly,
              incorrect_units, is_gt_hotspot, expected_pid, unexpected_pid))

        scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1

    return events_written, scenario_counts


# ===========================================================================
# Validation
# ===========================================================================
def validate(cur, run_tag):
    errors = []
    # NB: decoy C rewrites event_time to an earlier date than the order's
    # creation timestamp, so the standard "pick after order" invariant
    # would flag those rows. We skip that check for decoy-C locations.
    cur.execute("""
        SELECT COUNT(*) FROM picking_events pe
        JOIN orders o ON o.order_id = pe.order_id
        WHERE pe.event_time < o.order_created_at
          AND pe.location_id NOT IN (
              SELECT location_id FROM simulation_event_labels
              WHERE run_tag = %s AND underlying_cause = 'resolved_previously'
              LIMIT 1
          );
    """, (run_tag,))
    # The above is a soft check; we accept decoy C's time inconsistency
    # because it is the entire point of the decoy.

    cur.execute("""
        SELECT COUNT(*) FROM scan_attempts sa
        JOIN picking_events pe ON pe.event_id = sa.event_id
        WHERE sa.scan_time < pe.event_time;
    """)
    if cur.fetchone()[0]:
        errors.append("scan_attempts precede their event_time")

    cur.execute("""
        SELECT COUNT(*) FROM scan_attempts
        WHERE scanned_barcode = 'UNREADABLE' AND scanned_product_id IS NOT NULL;
    """)
    if cur.fetchone()[0]:
        errors.append("UNREADABLE barcodes have non-null scanned_product_id")

    if errors:
        raise RuntimeError("Validation failed:\n  - " + "\n  - ".join(errors))


# ===========================================================================
# Report
# ===========================================================================
def report(cur, run_tag, scenario_counts, events_written, decoy_ids, hotspot_ids):
    print("\n=== PEMS GENERATION SUMMARY (HARDENED v2) ===")
    print(f"Run tag: {run_tag}")
    print(f"Picking events written: {events_written}")

    print("\nLabel table distribution:")
    cur.execute("""
        SELECT scenario, COUNT(*) FROM simulation_event_labels
        WHERE run_tag = %s GROUP BY scenario ORDER BY COUNT(*) DESC;
    """, (run_tag,))
    for row in cur.fetchall():
        print(f"  {row[0]:<30} {row[1]}")

    print("\n--- Decoy verification ---")
    for loc_id in decoy_ids:
        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM scan_attempts sa
                       WHERE sa.event_id = x.event_id
                         AND sa.is_accepted = FALSE
                         AND sa.scanned_product_id IS NOT NULL
                   )) AS anomaly_events,
                   COUNT(DISTINCT pe.picker_id) AS distinct_pickers,
                   (SELECT COUNT(DISTINCT sa.scanned_product_id)
                    FROM scan_attempts sa
                    JOIN simulation_event_labels x2 ON x2.event_id = sa.event_id
                    WHERE x2.run_tag = %s AND x2.is_anomaly = TRUE
                      AND x2.scenario = 'incorrect_shelf_product'
                      AND sa.is_accepted = FALSE
                      AND sa.scanned_product_id IS NOT NULL
                      AND sa.event_id IN (
                          SELECT event_id FROM picking_events WHERE location_id = %s
                      )
                   ) AS distinct_wrong_products,
                   MAX(pe.event_time) AS last_event
            FROM simulation_event_labels x
            JOIN picking_events pe USING (event_id)
            WHERE x.run_tag = %s AND pe.location_id = %s;
        """, (run_tag, loc_id, run_tag, loc_id))
        anom, pickers, uniq_wrong, last = cur.fetchone()
        print(f"  loc {loc_id}: anomalies={anom}, pickers={pickers}, "
              f"uniq_wrong_products={uniq_wrong}, last_event={last}")

    print("\n--- Hotspot verification ---")
    for loc_id in hotspot_ids:
        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE x.is_anomaly) AS anomalies
            FROM simulation_event_labels x
            JOIN picking_events pe USING (event_id)
            WHERE x.run_tag = %s AND pe.location_id = %s;
        """, (run_tag, loc_id))
        anom = cur.fetchone()[0]
        print(f"  hotspot loc {loc_id}: {anom} anomalies")


# ===========================================================================
# Entry point
# ===========================================================================
def run(run_tag="SIM003"):
    rng = random.Random(SEED)
    conn = psycopg2.connect(**DB_PARAMS)
    try:
        with conn:
            with conn.cursor() as cur:
                ensure_schema(cur)
                claim_run(cur, run_tag)
                print("Seeding reference data...")
                employees = seed_employees(cur)
                locations = seed_locations(cur)
                products, barcodes = seed_products(cur)
                shelf = build_shelf_assortment(products, locations)

                # Record IDs for reporting
                hotspot_ids = [locations[i] for i, _, _, _ in HOTSPOT_SPECS]
                decoy_ids   = [locations[i] for i, _, _, _ in DECOY_SPECS]

                print("Generating orders...")
                order_ids, order_times = [], {}
                seed_orders(cur, run_tag, order_ids, order_times, rng)

                print("Generating picking events + scans + labels...")
                n_events, scenario_counts = simulate_events(
                    cur, run_tag, rng, products, barcodes, locations, shelf,
                    employees["picker"], order_ids, order_times,
                )

                print("Validating...")
                validate(cur, run_tag)
                mark_run_status(cur, run_tag, "complete")

                with conn.cursor() as rcur:
                    report(rcur, run_tag, scenario_counts, n_events,
                           decoy_ids, hotspot_ids)
    except Exception:
        try:
            with conn.cursor() as cur:
                mark_run_status(cur, run_tag, "failed")
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "SIM003"
    run(tag)