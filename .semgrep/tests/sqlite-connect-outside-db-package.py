import sqlite3 as sqlite_module
from sqlite3 import connect as open_database


def unrelated_connect(_: str) -> None:
    pass


def open_databases() -> None:
    # ruleid: sqlite-connect-outside-db-package
    sqlite_module.connect(":memory:")
    # ruleid: sqlite-connect-outside-db-package
    open_database(":memory:")


def near_misses() -> None:
    # ok: sqlite-connect-outside-db-package
    sqlite_module.Row
    # ok: sqlite-connect-outside-db-package
    unrelated_connect(":memory:")
