"""
ActiveRecord-style mixin for SQLModel models.

This module provides Rails-inspired ActiveRecord patterns for SQLModel/SQLAlchemy,
improving developer ergonomics for database operations in the LlamaBot console
and throughout the application.

Philosophy
----------
Ruby on Rails' ActiveRecord pattern treats database tables as classes and rows as
instances, providing an intuitive API for CRUD operations. While SQLModel uses
SQLAlchemy's Data Mapper pattern (explicit session-based queries), this mixin
bridges the gap by adding familiar ActiveRecord-style methods.

This is especially useful for:
- Interactive console sessions (like `rails console`)
- Quick prototyping and debugging
- Developers familiar with Rails conventions

Design Decisions
----------------
1. **Global Session**: Uses a module-level session reference set by console.py.
   This mirrors Rails' implicit database connection while keeping the explicit
   session available for production code.

2. **Class Methods for Queries**: `Model.all()`, `Model.find(id)`, etc. provide
   the familiar Rails API without modifying SQLModel's core behavior.

3. **Instance Methods for Mutations**: `record.save()`, `record.update()`,
   `record.destroy()` handle persistence with automatic timestamp updates.

4. **Chainable Returns**: Methods return `self` where appropriate, enabling
   method chaining like `User.find(1).update(role='admin')`.

Limitations
-----------
- ActiveRecord methods require `set_console_session()` to be called first
- Not suitable for async contexts (use SQLModel's native patterns instead)
- The `where()` method returns a list, not a chainable query object
- DO NOT use ActiveRecordMixin methods in production flows. These are console-only helpers.

Usage
-----
1. Inherit from ActiveRecordMixin in your model:

    ```python
    from app.lib import ActiveRecordMixin

    class User(ActiveRecordMixin, SQLModel, table=True):
        id: Optional[int] = Field(default=None, primary_key=True)
        username: str
        # ...
    ```

2. In the console (session is auto-configured):

    ```python
    >>> User.all()
    >>> User.find(1)
    >>> User.find_by(username='admin')
    >>> User.where(User.is_active == True)
    >>> user = User.first()
    >>> user.update(role='admin')
    >>> user.destroy()
    ```

3. In application code, prefer explicit sessions:

    ```python
    with Session(engine) as session:
        user = session.exec(select(User).where(User.id == 1)).first()
    ```

See Also
--------
- Rails ActiveRecord: https://guides.rubyonrails.org/active_record_basics.html
- SQLModel: https://sqlmodel.tiangolo.com/
- SQLAlchemy Session: https://docs.sqlalchemy.org/en/20/orm/session.html
"""

from datetime import datetime, timezone
from typing import Optional, List, TypeVar, TYPE_CHECKING
from sqlmodel import select

if TYPE_CHECKING:
    from sqlmodel import Session


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------

_console_session: Optional["Session"] = None
"""Global session reference for ActiveRecord-style methods.

Set by console.py on startup. This enables Rails-like implicit database
access in interactive sessions while keeping explicit sessions for
production code.
"""

T = TypeVar("T", bound="ActiveRecordMixin")
"""Type variable for generic return types in mixin methods."""


def set_console_session(session: "Session") -> None:
    """Set the global session for ActiveRecord-style class methods.

    Called automatically by console.py on startup. You typically don't
    need to call this directly.

    Args:
        session: SQLModel Session instance to use for all ActiveRecord operations.

    Example:
        >>> from sqlmodel import Session
        >>> from app.db import engine
        >>> from app.lib import set_console_session
        >>> set_console_session(Session(engine))
    """
    global _console_session
    _console_session = session


def get_console_session() -> "Session":
    """Get the global session for ActiveRecord operations.

    Returns:
        The current SQLModel Session.

    Raises:
        RuntimeError: If no session has been set via set_console_session().
    """
    if _console_session is None:
        raise RuntimeError(
            "No session available. Use set_console_session(session) first, "
            "or use session.exec(select(Model)) directly."
        )
    return _console_session


# ---------------------------------------------------------------------------
# ActiveRecord Mixin
# ---------------------------------------------------------------------------


class ActiveRecordMixin:
    """Mixin providing Rails-style ActiveRecord methods for SQLModel models.

    Add this mixin to any SQLModel class to get familiar Rails-style methods
    for querying and manipulating records. The mixin must come before SQLModel
    in the inheritance chain.

    Class Methods (Querying):
        all()              - Get all records
        first()            - Get first record (by id)
        second()           - Get second record (by id)
        third()            - Get third record (by id)
        fourth()           - Get fourth record (by id)
        fifth()            - Get fifth record (by id)
        last()             - Get last record (by id)
        find(id)           - Find by ID (raises if not found)
        find_by(**kwargs)  - Find first matching record
        where(*conditions) - Filter records by conditions
        count()            - Count all records

    Instance Methods (Persistence):
        save()             - Save changes to database
        update(**kwargs)   - Update attributes and save
        destroy()          - Delete record from database
        reload()           - Refresh from database

    Example:
        ```python
        class Post(ActiveRecordMixin, SQLModel, table=True):
            id: Optional[int] = Field(default=None, primary_key=True)
            title: str
            published: bool = False

        # Querying
        Post.all()                           # All posts
        Post.find(1)                         # Find by ID
        Post.find_by(title='Hello')          # Find by attribute
        Post.where(Post.published == True)   # Filter

        # Persistence
        post = Post(title='New Post')
        post.save()                          # Insert
        post.update(published=True)          # Update
        post.destroy()                       # Delete
        ```

    Note:
        Requires set_console_session() to be called first. This is done
        automatically when using the LlamaBot console.
    """

    # -----------------------------------------------------------------------
    # Class Methods - Querying
    # -----------------------------------------------------------------------

    @classmethod
    def all(cls: type[T]) -> List[T]:
        """Return all records.

        Equivalent to Rails: `Model.all`

        Returns:
            List of all records in the table.

        Example:
            >>> User.all()
            [User(id=1, ...), User(id=2, ...)]
        """
        session = get_console_session()
        return list(session.exec(select(cls)).all())

    @classmethod
    def first(cls: type[T]) -> Optional[T]:
        """Return first record ordered by ID.

        Equivalent to Rails: `Model.first`

        Returns:
            First record or None if table is empty.

        Example:
            >>> User.first()
            User(id=1, username='admin', ...)
        """
        session = get_console_session()
        return session.exec(select(cls).order_by(cls.id)).first()

    @classmethod
    def second(cls: type[T]) -> Optional[T]:
        """Return second record ordered by ID.

        Equivalent to Rails: `Model.second`

        Returns:
            Second record or None if fewer than 2 records exist.

        Example:
            >>> User.second()
            User(id=2, username='second_user', ...)
        """
        session = get_console_session()
        return session.exec(select(cls).order_by(cls.id).offset(1)).first()

    @classmethod
    def third(cls: type[T]) -> Optional[T]:
        """Return third record ordered by ID.

        Equivalent to Rails: `Model.third`

        Returns:
            Third record or None if fewer than 3 records exist.

        Example:
            >>> User.third()
            User(id=3, username='third_user', ...)
        """
        session = get_console_session()
        return session.exec(select(cls).order_by(cls.id).offset(2)).first()

    @classmethod
    def fourth(cls: type[T]) -> Optional[T]:
        """Return fourth record ordered by ID.

        Equivalent to Rails: `Model.fourth`

        Returns:
            Fourth record or None if fewer than 4 records exist.

        Example:
            >>> User.fourth()
            User(id=4, username='fourth_user', ...)
        """
        session = get_console_session()
        return session.exec(select(cls).order_by(cls.id).offset(3)).first()

    @classmethod
    def fifth(cls: type[T]) -> Optional[T]:
        """Return fifth record ordered by ID.

        Equivalent to Rails: `Model.fifth`

        Returns:
            Fifth record or None if fewer than 5 records exist.

        Example:
            >>> User.fifth()
            User(id=5, username='fifth_user', ...)
        """
        session = get_console_session()
        return session.exec(select(cls).order_by(cls.id).offset(4)).first()

    @classmethod
    def last(cls: type[T]) -> Optional[T]:
        """Return last record ordered by ID.

        Equivalent to Rails: `Model.last`

        Returns:
            Last record or None if table is empty.

        Example:
            >>> User.last()
            User(id=42, username='newest_user', ...)
        """
        session = get_console_session()
        return session.exec(select(cls).order_by(cls.id.desc())).first()

    @classmethod
    def find(cls: type[T], id: int) -> T:
        """Find record by ID, raising an error if not found.

        Equivalent to Rails: `Model.find(id)`

        Args:
            id: Primary key value to search for.

        Returns:
            The record with the given ID.

        Raises:
            ValueError: If no record with the given ID exists.

        Example:
            >>> User.find(1)
            User(id=1, username='admin', ...)
            >>> User.find(999)
            ValueError: User with id=999 not found
        """
        session = get_console_session()
        record = session.get(cls, id)
        if record is None:
            raise ValueError(f"{cls.__name__} with id={id} not found")
        return record

    @classmethod
    def find_by(cls: type[T], **kwargs) -> Optional[T]:
        """Find first record matching the given attributes.

        Equivalent to Rails: `Model.find_by(attribute: value)`

        Args:
            **kwargs: Attribute names and values to match.

        Returns:
            First matching record or None.

        Example:
            >>> User.find_by(username='admin')
            User(id=1, username='admin', ...)
            >>> User.find_by(username='nonexistent')
            None
        """
        session = get_console_session()
        stmt = select(cls)
        for key, value in kwargs.items():
            stmt = stmt.where(getattr(cls, key) == value)
        return session.exec(stmt).first()

    @classmethod
    def where(cls: type[T], *conditions) -> List[T]:
        """Filter records by SQLAlchemy conditions.

        Equivalent to Rails: `Model.where(conditions)`

        Note: Unlike Rails, this returns a list immediately rather than
        a chainable query object.

        Args:
            *conditions: SQLAlchemy filter conditions.

        Returns:
            List of matching records.

        Example:
            >>> User.where(User.is_admin == True)
            [User(id=1, is_admin=True, ...)]
            >>> User.where(User.role == 'engineer', User.is_active == True)
            [User(id=2, ...), User(id=3, ...)]
        """
        session = get_console_session()
        stmt = select(cls)
        for condition in conditions:
            stmt = stmt.where(condition)
        return list(session.exec(stmt).all())

    @classmethod
    def count(cls) -> int:
        """Count all records in the table.

        Equivalent to Rails: `Model.count`

        Returns:
            Number of records.

        Example:
            >>> User.count()
            42
        """
        return len(cls.all())

    # -----------------------------------------------------------------------
    # Instance Methods - Persistence
    # -----------------------------------------------------------------------

    def save(self: T) -> T:
        """Save the record to the database.

        Equivalent to Rails: `record.save`

        If the record has an `updated_at` attribute, it will be automatically
        set to the current UTC time.

        Returns:
            self (for method chaining)

        Example:
            >>> user = User(username='new_user', password_hash='...')
            >>> user.save()
            User(id=43, username='new_user', ...)
        """
        session = get_console_session()
        if hasattr(self, "updated_at"):
            self.updated_at = datetime.now(timezone.utc)
        session.add(self)
        session.commit()
        session.refresh(self)
        return self

    def update(self: T, **kwargs) -> T:
        """Update attributes and save the record.

        Equivalent to Rails: `record.update(attributes)`

        Args:
            **kwargs: Attribute names and new values.

        Returns:
            self (for method chaining)

        Raises:
            AttributeError: If an attribute doesn't exist on the model.

        Example:
            >>> user = User.find(1)
            >>> user.update(is_admin=True, role='admin')
            User(id=1, is_admin=True, role='admin', ...)
        """
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                raise AttributeError(
                    f"{type(self).__name__} has no attribute '{key}'"
                )
        return self.save()

    def destroy(self) -> bool:
        """Delete the record from the database.

        Equivalent to Rails: `record.destroy`

        Returns:
            True if successful.

        Example:
            >>> user = User.find(1)
            >>> user.destroy()
            True
        """
        session = get_console_session()
        session.delete(self)
        session.commit()
        return True

    def reload(self: T) -> T:
        """Reload the record from the database.

        Equivalent to Rails: `record.reload`

        Useful after external changes or to discard unsaved modifications.

        Returns:
            self (for method chaining)

        Example:
            >>> user = User.find(1)
            >>> user.username = 'changed'
            >>> user.reload()  # Discards the change
            User(id=1, username='original', ...)
        """
        session = get_console_session()
        session.refresh(self)
        return self
