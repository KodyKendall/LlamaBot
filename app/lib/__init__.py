"""
LlamaBot Library - Core framework extensions.

This package contains utilities and mixins that improve developer ergonomics,
taking inspiration from Ruby on Rails' "Convention over Configuration" philosophy.
"""

from app.lib.active_record import ActiveRecordMixin, set_console_session

__all__ = ["ActiveRecordMixin", "set_console_session"]
