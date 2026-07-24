import os
from sqlalchemy import create_engine, text

url = (
    f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)
engine = create_engine(url)
with engine.connect() as conn:
    conn.execute(text("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS extra_data JSONB DEFAULT NULL"))
    conn.commit()
print("extra_data column added to Render DB")