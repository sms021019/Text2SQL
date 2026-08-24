"""Redis-backed caching layer.

`app/cache/**` is the only place in the codebase allowed to import `redis`
(mirrors `app/core/**` staying a plain-Python layer -- see
`app/core/errors.py`). Nothing wires this into the app yet; that's Phase 2's
later tasks.
"""
