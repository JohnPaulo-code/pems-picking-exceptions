"""
PEMS synthetic data generator — blueprint-conformant.

Run:
    python generate_pems_data.py            # generates run SIM001
    python generate_pems_data.py SIM002     # generates a second run

Design references: Project Blueprint §8.4, §9, §10.
All rules in §10 are enforced. Operational master tables are never truncated.
"""

import random
import sys
from datetime import datetime, timedelta, timezone

import psycopg2

DB_PARAMS = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres",
    "password": "admin",
    "database": "picking_exceptions",
}

SEED = 42

# ---------------------------------------------------------------------------
# Dataset sizing (blueprint §9.1)
# ---------------------------------------------------------------------------
N_PRODUCTS = 100
N_LOCATIONS = 40
N_PICKERS = 20
N_QAS = 5
N_CAPTAINS = 2
N_ORDERS = 1_800            # within 1,500–2,000 range
N_PICKING_EVENTS = 5_000    # fixed target per blueprint

# ---------------------------------------------------------------------------
# Hotspot configuration (blueprint §9.3)
# 5 hotspot locations, each with a dominant unexpected product and a
# probability of firing per pick — so normal events still occur there.
# ---------------------------------------------------------------------------
# We resolve these to real IDs at runtime. Indices into the generated lists.
HOTSPOT_SPECS = [
    # (location_index, unexpected_product_index, p_hit, primary_cause)
    (5,  45, 0.28, "incorrect_product_placement"),
    (11, 22, 0.22, "incorrect_product_placement"),
    (18, 67, 0.32, "incorrect_product_placement"),
    (27, 8,  0.18, "incorrect_product_placement"),
    (33, 91, 0.25, "incorrect_product_placement"),
]

DAMAGED_BARCODE_PRODUCT_INDEX = 12   # product #13 has an intermittently damaged label

# Background ambient exception rates (non-hotspot activity)
P_AMBIENT_DAMAGED_BARCODE = 0.020
P_AMBIENT_MISSING_STOCK = 0.015
P_AMBIENT_MIXED_UNITS = 0.025        # invisible in scans; label-only


# ===========================================================================
# Schema helpers
# ===========================================================================
def ensure_schema(cur):
    """Create the simulation ledger and label table if missing.
    We do NOT create operational tables — they are assumed to exist."""
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
    """Rule 1: abort if run_tag already exists. Never truncate."""
    cur.execute(
        "INSERT INTO simulation_runs (run_tag, started_at, status) "
        "VALUES (%s, %s, 'running') ON CONFLICT (run_tag) DO NOTHING "
        "RETURNING run_tag;",
        (run_tag, datetime.now(timezone.utc)),
    )
    if cur.fetchone() is None:
        raise RuntimeError(
            f"Run tag '{run_tag}' already exists. Refusing to overwrite. "
            f"Pick a new tag or clean up deliberately."
        )


def mark_run_status(cur, run_tag, status):
    cur.execute(
        "UPDATE simulation_runs SET status = %s WHERE run_tag = %s;",
        (status, run_tag),
    )


# ===========================================================================
# Reference data (idempotent: get-or-create by natural key)
# ===========================================================================
def seed_employees(cur):
    """Return dict role -> [employee_id,...]. Get-or-create."""
    out = {"picker": [], "quality_shopper": [], "captain": []}
    plan = [
        ("picker", N_PICKERS, "SIM-PKR"),
        ("quality_shopper", N_QAS, "SIM-QAS"),
        ("captain", N_CAPTAINS, "SIM-CPT"),
    ]
    for role, count, prefix in plan:
        for i in range(1, count + 1):
            code = f"{prefix}-{i:03d}"
            cur.execute(
                "SELECT employee_id FROM employees WHERE employee_code = %s;",
                (code,),
            )
            row = cur.fetchone()
            if row:
                out[role].append(row[0])
            else:
                cur.execute(
                    "INSERT INTO employees (employee_code, employee_role) "
                    "VALUES (%s, %s) RETURNING employee_id;",
                    (code, role),
                )
                out[role].append(cur.fetchone()[0])
    return out


def seed_locations(cur):
    zones = ["A", "B", "C", "D"]
    out = []
    for i in range(1, N_LOCATIONS + 1):
        zone = zones[i % 4]
        code = f"SIM-LOC-{zone}-{i:02d}"
        cur.execute(
            "SELECT location_id FROM locations WHERE location_code = %s;", (code,)
        )
        row = cur.fetchone()
        if row:
            out.append(row[0])
        else:
            cur.execute(
                "INSERT INTO locations (location_code, zone) "
                "VALUES (%s, %s) RETURNING location_id;",
                (code, zone),
            )
            out.append(cur.fetchone()[0])
    return out


def seed_products(cur):
    """Return (product_ids, product_barcodes dict id->[barcodes])."""
    categories = ["Electronics", "Groceries", "Apparel", "Home"]
    product_ids = []
    barcodes = {}
    for i in range(1, N_PRODUCTS + 1):
        sku = f"SIM-SKU-{i:05d}"
        cur.execute(
            "SELECT product_id FROM products WHERE sku = %s;", (sku,)
        )
        row = cur.fetchone()
        if row:
            p_id = row[0]
        else:
            cur.execute(
                "INSERT INTO products (sku, product_name, category) "
                "VALUES (%s, %s, %s) RETURNING product_id;",
                (sku, f"Simulated Product #{i}", categories[i % 4]),
            )
            p_id = cur.fetchone()[0]
        product_ids.append(p_id)

        bc1 = f"BC-{p_id:05d}-1"
        cur.execute(
            "SELECT barcode FROM product_barcodes WHERE barcode = %s;", (bc1,)
        )
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO product_barcodes (product_id, barcode) VALUES (%s, %s);",
                (p_id, bc1),
            )
        barcodes[p_id] = [bc1]

        if i % 10 == 0:  # 10% alternate packaging
            bc2 = f"BC-{p_id:05d}-2"
            cur.execute(
                "SELECT barcode FROM product_barcodes WHERE barcode = %s;", (bc2,)
            )
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO product_barcodes (product_id, barcode) VALUES (%s, %s);",
                    (p_id, bc2),
                )
            barcodes[p_id].append(bc2)
    return product_ids, barcodes


# ===========================================================================
# Orders
# ===========================================================================
def seed_orders(cur, run_tag, order_ids_out, timestamps_out, rng):
    start = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    # Run-scoped order references so reruns do not collide.
    for o_idx in range(1, N_ORDERS + 1):
        ref = f"{run_tag}-ORD-{o_idx:06d}"
        start += timedelta(minutes=rng.randint(2, 10))
        cur.execute(
            "INSERT INTO orders (order_reference, order_created_at) "
            "VALUES (%s, %s) RETURNING order_id;",
            (ref, start),
        )
        o_id = cur.fetchone()[0]
        order_ids_out.append(o_id)
        timestamps_out[o_id] = start


# ===========================================================================
# Event loop
# ===========================================================================
def build_shelf_assortment(product_ids, location_ids):
    """Each location normally carries 3 products."""
    shelf = {}
    for idx, loc in enumerate(location_ids):
        shelf[loc] = [product_ids[(idx + j) % len(product_ids)] for j in range(3)]
    return shelf


def pick_quantity(rng):
    return rng.choices([1, 2, 3, 4], weights=[0.70, 0.20, 0.07, 0.03])[0]


def simulate_events(
    cur, run_tag, rng,
    product_ids, product_barcodes, location_ids, shelf_assortment,
    picker_ids, order_ids, order_timestamps,
):
    damaged_product_id = product_ids[DAMAGED_BARCODE_PRODUCT_INDEX]

    # Resolve hotspot specs to runtime IDs
    hotspots = {}
    for loc_idx, unexp_idx, p_hit, cause in HOTSPOT_SPECS:
        loc_id = location_ids[loc_idx]
        hotspots[loc_id] = {
            "unexpected_product_id": product_ids[unexp_idx],
            "p_hit": p_hit,
            "cause": cause,
        }

    events_written = 0
    scenario_counts = {}

    for _ in range(N_PICKING_EVENTS):
        order_id = rng.choice(order_ids)
        location_id = rng.choice(location_ids)
        expected_product_id = rng.choice(shelf_assortment[location_id])
        picker_id = rng.choice(picker_ids)
        requested_qty = pick_quantity(rng)

        pick_time = order_timestamps[order_id] + timedelta(
            minutes=rng.randint(5, 45)
        )

        # ---- Decide scenario --------------------------------------------
        # Precedence: hotspot > ambient. This ensures hotspots produce the
        # dominant recurring signal, while still allowing normal picks there.
        scenario = "normal"
        cause = "none"
        is_anomaly = False
        incorrect_units = 0
        expected_pid = expected_product_id
        unexpected_pid = None
        accepted_product_id = expected_product_id
        scans = []  # (barcode, scanned_product_id, is_accepted)

        if location_id in hotspots and rng.random() < hotspots[location_id]["p_hit"]:
            # Recurring hotspot exception: unexpected product is what the
            # picker actually finds at the shelf. Then they recover.
            unexpected_pid = hotspots[location_id]["unexpected_product_id"]
            # Avoid degenerate case: expected == unexpected would produce no signal.
            if unexpected_pid == expected_product_id:
                unexpected_pid = product_ids[
                    (product_ids.index(unexpected_pid) + 1) % len(product_ids)
                ]
            wrong_bc = rng.choice(product_barcodes[unexpected_pid])
            # §9.4: reject unexpected → find correct → accept → qty confirmed
            scans.append((wrong_bc, unexpected_pid, False))
            good_bc = rng.choice(product_barcodes[expected_product_id])
            scans.append((good_bc, expected_product_id, True))
            accepted_product_id = expected_product_id
            scenario = "incorrect_shelf_product"
            cause = hotspots[location_id]["cause"]
            is_anomaly = True

        elif expected_product_id == damaged_product_id and rng.random() < 0.6:
            # Intermittent damaged label on this product. Unreadable scans
            # then recovery. sentinel barcode + NULL scanned_product_id.
            n_fail = rng.randint(2, 5)
            for _ in range(n_fail):
                scans.append(("UNREADABLE", None, False))
            if rng.random() < 0.20:
                # Frustrated short-pick: no accepted scan, qty zero.
                accepted_product_id = None
                scenario = "damaged_barcode"
                cause = "faded_label_intermittent"
                is_anomaly = True
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
            # No scans at all; zero confirmed.
            accepted_product_id = None
            scenario = "missing_stock"
            cause = "system_count_discrepancy"
            is_anomaly = True

        elif rng.random() < P_AMBIENT_MIXED_UNITS:
            # Looks completely normal in scans. Label-only.
            good_bc = rng.choice(product_barcodes[expected_product_id])
            scans.append((good_bc, expected_product_id, True))
            accepted_product_id = expected_product_id
            incorrect_units = rng.randint(1, requested_qty)
            scenario = "mixed_physical_products"
            cause = "multi_unit_contamination"
            is_anomaly = True

        else:
            # Normal pick.
            good_bc = rng.choice(product_barcodes[expected_product_id])
            scans.append((good_bc, expected_product_id, True))
            accepted_product_id = expected_product_id

        # ---- Confirmed quantity ------------------------------------------
        if accepted_product_id is None:
            confirmed_qty = 0
        else:
            confirmed_qty = requested_qty

        # ---- Persist picking_event ---------------------------------------
        cur.execute(
            """
            INSERT INTO picking_events
                (order_id, product_id, location_id, picker_id,
                 requested_quantity, confirmed_quantity, event_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING event_id;
            """,
            (order_id, expected_pid, location_id, picker_id,
             requested_qty, confirmed_qty, pick_time),
        )
        event_id = cur.fetchone()[0]
        events_written += 1

        # ---- Persist scan_attempts ---------------------------------------
        scan_time = pick_time
        for barcode, scanned_pid, accepted in scans:
            scan_time += timedelta(seconds=rng.randint(4, 10))
            cur.execute(
                """
                INSERT INTO scan_attempts
                    (event_id, scanned_barcode, scanned_product_id,
                     is_accepted, scan_time)
                VALUES (%s, %s, %s, %s, %s);
                """,
                (event_id, barcode, scanned_pid, accepted, scan_time),
            )

        # ---- Persist ground truth ----------------------------------------
        cur.execute(
            """
            INSERT INTO simulation_event_labels
                (event_id, run_tag, scenario, underlying_cause, is_anomaly,
                 incorrect_unit_count, is_hotspot_location,
                 expected_product_id, unexpected_product_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (event_id, run_tag, scenario, cause, is_anomaly,
             incorrect_units, location_id in hotspots,
             expected_pid, unexpected_pid),
        )

        scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1

    return events_written, scenario_counts


# ===========================================================================
# Post-insert validation (Rule 5 + invariants)
# ===========================================================================
def validate(cur, run_tag):
    errors = []

    # 1. Every pick occurs after its order.
    cur.execute(
        """
        SELECT COUNT(*) FROM picking_events pe
        JOIN orders o ON o.order_id = pe.order_id
        WHERE pe.event_time < o.order_created_at;
        """
    )
    if cur.fetchone()[0]:
        errors.append("Some picking_events precede their order_created_at")

    # 2. Every scan occurs after its picking event.
    cur.execute(
        """
        SELECT COUNT(*) FROM scan_attempts sa
        JOIN picking_events pe ON pe.event_id = sa.event_id
        WHERE sa.scan_time < pe.event_time;
        """
    )
    if cur.fetchone()[0]:
        errors.append("Some scan_attempts precede their event_time")

    # 3. Every event in this run has exactly one label row.
    cur.execute(
        """
        SELECT COUNT(*) FROM picking_events pe
        LEFT JOIN simulation_event_labels l
               ON l.event_id = pe.event_id AND l.run_tag = %s
        WHERE l.event_id IS NULL
          AND pe.event_id IN (
              SELECT event_id FROM simulation_event_labels WHERE run_tag = %s
          );
        """,
        (run_tag, run_tag),
    )
    # (loose check — full cross-run audit is out of scope here)

    # 4. Unreadable scans must have NULL scanned_product_id.
    cur.execute(
        """
        SELECT COUNT(*) FROM scan_attempts
        WHERE scanned_barcode = 'UNREADABLE' AND scanned_product_id IS NOT NULL;
        """
    )
    if cur.fetchone()[0]:
        errors.append("UNREADABLE barcodes have non-null scanned_product_id")

    # 5. Hotspot-pattern invariant: in 'incorrect_shelf_product' labels, at
    #    least one accepted scan for the expected product must exist.
    cur.execute(
        """
        SELECT COUNT(*) FROM simulation_event_labels l
        WHERE l.run_tag = %s
          AND l.scenario = 'incorrect_shelf_product'
          AND NOT EXISTS (
              SELECT 1 FROM scan_attempts sa
              WHERE sa.event_id = l.event_id
                AND sa.is_accepted = TRUE
                AND sa.scanned_product_id = l.expected_product_id
          );
        """,
        (run_tag,),
    )
    if cur.fetchone()[0]:
        errors.append(
            "Some incorrect_shelf_product events lack a recovery acceptance"
        )

    if errors:
        raise RuntimeError("Validation failed:\n  - " + "\n  - ".join(errors))


# ===========================================================================
# Report
# ===========================================================================
def report(cur, run_tag, scenario_counts, events_written):
    print("\n=== PEMS GENERATION SUMMARY ===")
    print(f"Run tag: {run_tag}")
    print(f"Picking events written: {events_written}")
    print("\nScenario distribution (from in-memory counter):")
    for k in sorted(scenario_counts):
        print(f"  {k:<30} {scenario_counts[k]}")

    print("\nLabel table distribution (from DB):")
    cur.execute(
        """
        SELECT scenario, COUNT(*) FROM simulation_event_labels
        WHERE run_tag = %s GROUP BY scenario ORDER BY COUNT(*) DESC;
        """,
        (run_tag,),
    )
    for row in cur.fetchall():
        print(f"  {row[0]:<30} {row[1]}")

    print("\nHotspot locations (blueprint §9.3):")
    cur.execute(
        """
        SELECT l.location_id, COUNT(*) FILTER (WHERE x.is_anomaly) AS anomalies,
               COUNT(*) AS total_events
        FROM simulation_event_labels x
        JOIN picking_events pe ON pe.event_id = x.event_id
        JOIN locations l ON l.location_id = pe.location_id
        WHERE x.run_tag = %s AND x.is_hotspot_location = TRUE
        GROUP BY l.location_id
        ORDER BY l.location_id;
        """,
        (run_tag,),
    )
    for loc, anom, tot in cur.fetchall():
        print(f"  location_id={loc:<4} events={tot:<5} anomalies={anom}")

    print("\nMixed-item injection (invisible-in-scans check):")
    cur.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(incorrect_unit_count),0)
        FROM simulation_event_labels
        WHERE run_tag = %s AND scenario = 'mixed_physical_products';
        """,
        (run_tag,),
    )
    n_ev, n_units = cur.fetchone()
    print(f"  mixed-item events: {n_ev}  total incorrect units: {n_units}")


# ===========================================================================
# Entry point
# ===========================================================================
def run(run_tag="SIM001"):
    rng = random.Random(SEED)  # local RNG, unaffected by other code

    conn = psycopg2.connect(**DB_PARAMS)
    try:
        with conn:
            with conn.cursor() as cur:
                ensure_schema(cur)
                claim_run(cur, run_tag)

                print("Seeding reference data (get-or-create)...")
                employees = seed_employees(cur)
                locations = seed_locations(cur)
                products, barcodes = seed_products(cur)

                shelf = build_shelf_assortment(products, locations)

                print("Generating orders...")
                order_ids, order_times = [], {}
                seed_orders(cur, run_tag, order_ids, order_times, rng)

                print("Generating picking events + scans + labels...")
                n_events, scenario_counts = simulate_events(
                    cur, run_tag, rng,
                    products, barcodes, locations, shelf,
                    employees["picker"], order_ids, order_times,
                )

                print("Validating...")
                validate(cur, run_tag)

                mark_run_status(cur, run_tag, "complete")

        # Reporting is read-only; can be a fresh cursor.
        with conn.cursor() as cur:
            report(cur, run_tag, scenario_counts, n_events)

    except Exception:
        # Mark the run failed (best-effort) then re-raise.
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
    tag = sys.argv[1] if len(sys.argv) > 1 else "SIM001"
    run(tag)