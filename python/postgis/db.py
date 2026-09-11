from __future__ import annotations

import os

from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def get_required_environment_variable(
    name: str,
) -> str:
    """
    Read and validate a required environment variable.
    """

    value = os.getenv(name)

    if value is None or not value.strip():
        raise EnvironmentError(f"{name} is not set.")

    return value.strip()


def create_postgis_engine() -> Engine:
    """
    Create a SQLAlchemy engine for the PostgreSQL/PostGIS database.
    """

    host = get_required_environment_variable(
        "POSTGIS_HOST",
    )

    port = os.getenv(
        "POSTGIS_PORT",
        "5432",
    )

    database = get_required_environment_variable(
        "POSTGIS_DATABASE",
    )

    user = get_required_environment_variable(
        "POSTGIS_USER",
    )

    password = get_required_environment_variable(
        "POSTGIS_PASSWORD",
    )

    sslmode = os.getenv(
        "POSTGIS_SSLMODE",
        "prefer",
    )

    encoded_user = quote_plus(
        user,
    )

    encoded_password = quote_plus(
        password,
    )

    database_url = (
        f"postgresql+psycopg2://"
        f"{encoded_user}:"
        f"{encoded_password}@"
        f"{host}:"
        f"{port}/"
        f"{database}"
        f"?sslmode={sslmode}"
    )

    return create_engine(
        database_url,
        pool_pre_ping=True,
    )


def test_database_connection(
    engine: Engine,
) -> None:
    """
    Verify the PostgreSQL connection and PostGIS extension.
    """

    with engine.connect() as connection:
        database_name = connection.execute(
            text("SELECT current_database();")
        ).scalar_one()

        postgis_version = connection.execute(
            text("SELECT PostGIS_Version();")
        ).scalar_one()

    print(
        f"Connected to PostgreSQL "
        f"database={database_name} "
        f"PostGIS={postgis_version}"
    )
