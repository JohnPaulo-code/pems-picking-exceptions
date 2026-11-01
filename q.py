"""
Ad-hoc query runner for PEMS.
Usage: python q.py                      # runs all queries in queries.py
       python q.py "SELECT 1"           # runs a single query inline
"""
import sys
import psycopg2
from psycopg2.extras import RealDictCursor

DB_PARAMS = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "postgres",
    "password": "admin",
    "database": "picking_exceptions",
}


def run_one(cur, sql):
    cur.execute(sql)
    if cur.description is None:
        print(f"(no result set) status: {cur.statusmessage}")
        return
    rows = cur.fetchall()
    if not rows:
        print("(0 rows)")
        return
    cols = [d.name for d in cur.description]
    widths = [max(len(c), max(len(str(r[c])) for r in rows)) for c in cols]
    header = " | ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(header)
    print("-" * len(header))
    for r in rows:
        print(" | ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    print(f"({len(rows)} rows)")


def main():
    conn = psycopg2.connect(**DB_PARAMS)
    conn.autocommit = True
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if len(sys.argv) > 1:
                run_one(cur, " ".join(sys.argv[1:]))
            else:
                # Load queries.py from the same directory
                import importlib.util, pathlib
                spec = importlib.util.spec_from_file_location(
                    "queries", pathlib.Path(__file__).with_name("queries.py")
                )
                q = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(q)
                for name, sql in q.QUERIES:
                    print(f"\n=== {name} ===")
                    run_one(cur, sql)
    finally:
        conn.close()


if __name__ == "__main__":
    main()