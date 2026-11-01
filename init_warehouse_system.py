import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

DB_PARAMS = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres",
    "password": "admin"
}


def init_warehouse_system():
    # Phase 1: Create Database isolated from a transaction block
    conn = psycopg2.connect(**DB_PARAMS, database="postgres")
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cursor = conn.cursor()
    try:
        cursor.execute("CREATE DATABASE picking_exceptions;")
        print("Database 'picking_exceptions' created.")
    except psycopg2.errors.DuplicateDatabase:
        print("Database 'picking_exceptions' already exists. Re-verifying schema layout...")
    finally:
        cursor.close()
        conn.close()

    # Phase 2: Create Consolidated Tables directly inside picking_exceptions
    conn = psycopg2.connect(**DB_PARAMS, database="picking_exceptions")
    cursor = conn.cursor()

    schema_ddl = """
    -- 1. EMPLOYEES
    CREATE TABLE IF NOT EXISTS employees (
        employee_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        employee_code VARCHAR(20) UNIQUE NOT NULL,
        employee_role VARCHAR(30) NOT NULL CHECK (employee_role IN ('picker', 'quality_shopper', 'captain')),
        is_active BOOLEAN DEFAULT TRUE
    );

    -- 2. PRODUCTS
    CREATE TABLE IF NOT EXISTS products (
        product_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        sku VARCHAR(30) UNIQUE NOT NULL,
        product_name VARCHAR(150) NOT NULL,
        category VARCHAR(80)
    );

    -- 3. PRODUCT BARCODES
    CREATE TABLE IF NOT EXISTS product_barcodes (
        barcode_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
        barcode VARCHAR(50) UNIQUE NOT NULL,
        is_active BOOLEAN DEFAULT TRUE
    );

    -- 4. LOCATIONS
    CREATE TABLE IF NOT EXISTS locations (
        location_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        location_code VARCHAR(30) UNIQUE NOT NULL,
        zone VARCHAR(50),
        is_active BOOLEAN DEFAULT TRUE
    );

    -- 5. ORDERS
    CREATE TABLE IF NOT EXISTS orders (
        order_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        order_reference VARCHAR(40) UNIQUE NOT NULL,
        order_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- 6. PICKING EVENTS
    CREATE TABLE IF NOT EXISTS picking_events (
        event_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(order_id),
        product_id INTEGER NOT NULL REFERENCES products(product_id),
        location_id INTEGER NOT NULL REFERENCES locations(location_id),
        picker_id INTEGER NOT NULL REFERENCES employees(employee_id),
        requested_quantity INTEGER NOT NULL CHECK (requested_quantity > 0),
        confirmed_quantity INTEGER CHECK (confirmed_quantity IS NULL OR confirmed_quantity >= 0),
        event_time TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- 7. SCAN ATTEMPTS
    CREATE TABLE IF NOT EXISTS scan_attempts (
        scan_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES picking_events(event_id) ON DELETE CASCADE,
        scanned_barcode VARCHAR(50) NOT NULL,
        scanned_product_id INTEGER REFERENCES products(product_id),
        is_accepted BOOLEAN NOT NULL,
        scan_time TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- 8. QUALITY TASKS
    -- task_reason is TEXT (not VARCHAR(100)) because Module A writes
    -- multi-clause explainable reasons, e.g.
    -- "Repeated rejection of same unexpected product across 7 pickers over 3 days".
    CREATE TABLE IF NOT EXISTS quality_tasks (
        task_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        location_id INTEGER NOT NULL REFERENCES locations(location_id),
        created_by INTEGER REFERENCES employees(employee_id),
        assigned_shopper_id INTEGER REFERENCES employees(employee_id),
        task_reason TEXT,
        priority VARCHAR(20) CHECK (priority IN ('low', 'medium', 'high')),
        status VARCHAR(30) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- 9. QUALITY TASK OUTCOMES
    -- confirmed_cause includes 'missing_stock' to match blueprint §7 six-cause model.
    CREATE TABLE IF NOT EXISTS quality_task_outcomes (
        outcome_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        task_id INTEGER UNIQUE NOT NULL REFERENCES quality_tasks(task_id) ON DELETE CASCADE,
        confirmed_cause VARCHAR(50) NOT NULL CHECK (confirmed_cause IN (
            'incorrect_product_placement',
            'damaged_barcode',
            'mixed_products',
            'missing_stock',
            'no_problem_found',
            'other'
        )),
        resolution_notes TEXT,
        resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- 10. SIMULATION LEDGER (Rule 1: run-tag isolation, no truncation)
    CREATE TABLE IF NOT EXISTS simulation_runs (
        run_tag VARCHAR(32) PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'running'
    );

    -- 11. SIMULATION GROUND TRUTH (blueprint §8.4)
    -- Evaluation-only. Never exposed to the detection pipeline.
    CREATE TABLE IF NOT EXISTS simulation_event_labels (
        event_id INTEGER NOT NULL,
        run_tag VARCHAR(32) NOT NULL,
        scenario VARCHAR(50) NOT NULL,
        underlying_cause VARCHAR(50) NOT NULL,
        is_anomaly BOOLEAN NOT NULL,
        incorrect_unit_count INTEGER NOT NULL DEFAULT 0,
        is_hotspot_location BOOLEAN NOT NULL DEFAULT FALSE,
        expected_product_id INTEGER NOT NULL,
        unexpected_product_id INTEGER,
        PRIMARY KEY (event_id, run_tag),
        FOREIGN KEY (run_tag) REFERENCES simulation_runs(run_tag)
    );

    -- Indexes for Module A analysis paths
    CREATE INDEX IF NOT EXISTS idx_scan_attempts_event     ON scan_attempts(event_id);
    CREATE INDEX IF NOT EXISTS idx_scan_attempts_barcode   ON scan_attempts(scanned_barcode);
    CREATE INDEX IF NOT EXISTS idx_scan_attempts_product   ON scan_attempts(scanned_product_id);
    CREATE INDEX IF NOT EXISTS idx_picking_events_location ON picking_events(location_id);
    CREATE INDEX IF NOT EXISTS idx_picking_events_time     ON picking_events(event_time);
    CREATE INDEX IF NOT EXISTS idx_picking_events_picker   ON picking_events(picker_id);
    CREATE INDEX IF NOT EXISTS idx_labels_run_tag          ON simulation_event_labels(run_tag);
    """

    try:
        cursor.execute(schema_ddl)
        conn.commit()
        print("Schema successfully built/verified.")

        # Run Verification Query
        cursor.execute("""
            SELECT table_name, COUNT(*) AS column_count
            FROM information_schema.columns
            WHERE table_schema = 'public'
            GROUP BY table_name
            ORDER BY table_name;
        """)
        print("\n--- Structural Verification ---")
        for table, col_count in cursor.fetchall():
            print(f"Table: {table:<30} | Columns: {col_count}")

        # Explicit check that the two previously-flagged constraints are correct
        cursor.execute("""
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'quality_task_outcomes'::regclass
              AND contype = 'c';
        """)
        print("\n--- quality_task_outcomes CHECK constraints ---")
        for name, definition in cursor.fetchall():
            print(f"  {name}: {definition}")

        cursor.execute("""
            SELECT data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'quality_tasks'
              AND column_name = 'task_reason';
        """)
        dtype, maxlen = cursor.fetchone()
        effective = "TEXT" if maxlen is None else f"{dtype}({maxlen})"
        print(f"\nquality_tasks.task_reason type: {effective}")

    except Exception as e:
        conn.rollback()
        print(f"Error establishing schema mapping: {e}")
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    init_warehouse_system()