"""Gunicorn config for the attendance app (auto-loaded from the app dir).

The systemd service runs ``gunicorn app:app`` — Python's ``__main__`` never
executes there, so without this hook the auto-fetch scheduler thread would
only start when the first browser request arrived. With post_worker_init it
starts as soon as the worker boots: schedules fire with no UI open.

Keep the service at ONE worker (--threads only). Each worker process would
run its own scheduler thread and fire duplicate fetches.
"""


def post_worker_init(worker):
    from app import start_scheduler_once

    start_scheduler_once()
