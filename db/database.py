from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import json
import os

def load_db_config():
    config_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
    with open(config_path, 'r') as f:
        config = json.load(f)
    db_config = config["db"]
    user = db_config["user"]
    password = db_config["password"]
    host = db_config["host"]
    port = db_config["port"]
    database = db_config["db"]
    return "postgresql://{}:{}@{}:{}/{}".format(user, password, host, port, database)

DATABASE_URL = os.getenv("DATABASE_URL") or load_db_config()

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
