"""Application settings read from the environment.

Variables from a ``.env`` file in the working directory are loaded on import,
without overriding variables that are already set.

Attributes
----------
DB_FILE : str
    Path of the JSON database file, from ``SERIOUSDB_DB_FILE``.
    Defaults to ``.sdb``.
LOG_LEVEL : str
    Name of the logging level, from ``SERIOUSDB_LOG_LEVEL``.
    Defaults to ``INFO``.
HOST : str
    Host IP where the API is used, from ``SERIOUSDB_HOST``.
    Defaults to ``127.0.0.1``
PORT : int
    Host port which is used by the API, from ``SERIOUSDB_PORT``
    Defaults to ``8000``
"""

import os

from dotenv import load_dotenv

load_dotenv()

DB_FILE = os.getenv("SERIOUSDB_DB_FILE", ".sdb")
LOG_LEVEL = os.getenv("SERIOUSDB_LOG_LEVEL", "INFO")
HOST = os.getenv("SERIOUSDB_HOST", "127.0.0.1")
PORT = int(os.getenv("SERIOUSDB_PORT", "8000"))
