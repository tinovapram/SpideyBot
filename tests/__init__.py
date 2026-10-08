"""Test suite.

An ``__init__.py`` here is not decoration. Several dependencies ship a top-level ``tests``
package in site-packages, and a *regular* package beats an implicit namespace package
regardless of path order — so without this file ``import tests.fakes`` resolved into
site-packages and failed. Being a real package makes the resolution deterministic.
"""
