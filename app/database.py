from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tripdex.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(
    DATABASE_URL,
    connect_args={**connect_args, "detect_types": 1} if DATABASE_URL.startswith("sqlite") else connect_args
)

# Enable dict-like row access for raw SQL
from sqlalchemy import event
@event.listens_for(engine, "connect")
def set_sqlite_row_factory(dbapi_conn, _):
    import sqlite3
    dbapi_conn.row_factory = sqlite3.Row
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
