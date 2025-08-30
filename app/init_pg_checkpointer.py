# init_pg_checkpointer.py
import os   
from langgraph.checkpoint.postgres import PostgresSaver, ConnectionPool
from dotenv import load_dotenv
from psycopg import Connection

#https://github.com/langchain-ai/langgraph/issues/2887

load_dotenv()

db_uri = os.getenv("DB_URI")

if db_uri is None:
    print("DB_URI is not set, we'll use InMemoryCheckpointer instead!")
else: 
    print("DB_URI is set, we'll use PostgresSaver instead!")
    print("DB_URI: ", db_uri)

    # Create connection pool
    # pool = ConnectionPool(db_uri)
    conn = Connection.connect(db_uri, autocommit=True)


    checkpointer_already_initialized = False
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)",
            ('checkpoints',),
        )
        table_exists = cursor.fetchone()[0]
        print(f"Table 'checkpoints' exists: {table_exists}")
        checkpointer_already_initialized = table_exists

    # Create the saver
    checkpointer = PostgresSaver(conn)

    # This runs DDL like CREATE TABLE and CREATE INDEX
    # including CREATE INDEX CONCURRENTLY, which must be run outside a transaction
    if not checkpointer_already_initialized:
        checkpointer.setup()

    print("✅ Checkpointer tables & indexes initialized.")