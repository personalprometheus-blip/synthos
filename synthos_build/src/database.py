# database.py — compatibility shim
# Agents import 'from database import ...' but the module is retail_database.py.
# This file re-exports everything so existing agent imports work without changes.
from retail_database import *
from retail_database import (
    DB,
    get_db,
    get_customer_db,
    acquire_agent_lock,
    release_agent_lock,
    _wait_for_agent_lock,
    DB_PATH,
    AGENT_LOCK_FILE,
    PRIORITY_AGENTS,
    BACKOFF_CALLERS,
)
