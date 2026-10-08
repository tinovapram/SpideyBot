"""Test doubles shared by the acceptance layer.

Anything here stands in for something outside the process — Telegram, a remote host —
so that FR and AC behaviour can be driven without a network. Nothing here fakes the
database: that is a real Postgres, because the SQL *is* the thing under test.
"""
