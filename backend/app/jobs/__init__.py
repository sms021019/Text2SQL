"""Background jobs: the arq task functions (`tasks.py`) and the worker
entry point (`worker.py`, run as `arq app.jobs.worker.WorkerSettings`).

`app/jobs/**` and `app/api/v1/jobs.py` are the only places allowed to import
`arq`; `app/core/**` stays a plain-Python layer (see `app/core/errors.py`).
"""
