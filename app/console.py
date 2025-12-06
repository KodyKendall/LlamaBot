#!/usr/bin/env python
"""
LlamaBot Console - Interactive shell for database operations.

Similar to Rails Console, provides an IPython shell with pre-loaded
models and ActiveRecord-style methods for querying the auth database.

Usage:
    python console.py
    # or via wrapper:
    ./bin/llamabot_console
"""
import sys
import os

# Ensure app module is importable when run from /app/app directory
# Add parent directory so 'app' package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select
from app.db import engine
from app.models import User, set_console_session
from app.services.user_service import (
    hash_password,
    verify_password,
    get_user_by_username,
    get_user_by_id,
    create_user,
    create_admin_user,
    authenticate_user,
    get_all_users,
    update_user,
    delete_user,
)

# Create a global session for interactive use
if engine is None:
    print("ERROR: AUTH_DB_URI is not set. Cannot connect to database.")
    sys.exit(1)

session = Session(engine)

# Enable ActiveRecord-style methods on User class
set_console_session(session)


def reload_session():
    """Refresh the session to see latest database changes."""
    global session
    session.close()
    session = Session(engine)
    set_console_session(session)
    print("Session reloaded.")


def commit():
    """Commit the current session."""
    session.commit()
    print("Changes committed.")


def rollback():
    """Rollback the current session."""
    session.rollback()
    print("Changes rolled back.")


BANNER = """
================================================================================
  LlamaBot Console (Rails-style)
================================================================================

ActiveRecord-style methods on User:
  User.all()                    - Get all users
  User.first()                  - Get first user
  User.last()                   - Get last user
  User.find(id)                 - Find by ID (raises if not found)
  User.find_by(username='bob')  - Find by attribute
  User.where(User.is_admin == True)  - Filter by condition
  User.count()                  - Count records

Instance methods:
  user.save()                   - Save changes
  user.update(field=val)        - Update and save
  user.destroy()                - Delete record
  user.reload()                 - Reload from database

Session helpers:
  commit()                      - Commit session
  rollback()                    - Rollback session
  reload_session()              - Get fresh session

Also available: session, select, create_user(), create_admin_user(), etc.

Examples:
  >>> User.all()
  >>> User.find(1)
  >>> User.find_by(username='admin')
  >>> User.where(User.role == 'engineer')
  >>> user = User.first(); user.role = 'admin'; user.save()
  >>> User(username='bob', password_hash='...').save()

================================================================================
"""


def main():
    try:
        from IPython import embed
        embed(banner1=BANNER, colors="neutral")
    except ImportError:
        print("IPython not installed. Falling back to standard Python shell.")
        import code
        code.interact(banner=BANNER, local=globals())


if __name__ == "__main__":
    main()
