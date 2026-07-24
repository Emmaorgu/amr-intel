"""
Check constraints on the Render DB resistance_records table
and fix any missing schema issues.
Run: python fix_render_db.py
"""
import os
from sqlalchemy import create_engine, text

url = (
    f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)
engine = create_engine(url)

with engine.connect() as conn:
    # Check existing constraints
    r = conn.execute(text(
        "SELECT constraint_name, constraint_type "
        "FROM information_schema.table_constraints "
        "WHERE table_name='resistance_records'"
    ))
    print("=== resistance_records constraints ===")
    constraints = list(r)
    for row in constraints:
        print(f"  {row[0]} ({row[1]})")

    # Check ingestion_log columns
    r2 = conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='ingestion_log' ORDER BY ordinal_position"
    ))
    print("\n=== ingestion_log columns ===")
    for row in r2:
        print(f"  {row[0]}")

    # Add unique constraint if missing
    constraint_names = [row[0] for row in constraints]
    if 'uq_resistance_record' not in constraint_names:
        print("\nAdding missing uq_resistance_record constraint...")
        conn.execute(text("""
            ALTER TABLE resistance_records
            ADD CONSTRAINT uq_resistance_record
            UNIQUE (pathogen_name, antibiotic_name, country_iso3, year, quarter,
                    data_source, source_record_id)
        """))
        conn.commit()
        print("Constraint added.")
    else:
        print("\nuq_resistance_record already exists.")