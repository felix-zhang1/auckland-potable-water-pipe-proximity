from __future__ import annotations

import logging
import sys

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy.engine import Engine

from db import (
    create_postgis_engine,
    test_database_connection,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ANALYSIS_SQL_DIRECTORY = PROJECT_ROOT / "python" / "postgis" / "sql" / "analysis"

LOGGER = logging.getLogger("run_postgis_analysis")


def configure_logging() -> None:
    """
    Configure logging to both the console and a timestamped log file.
    """

    now = datetime.now(
        ZoneInfo(
            "Pacific/Auckland",
        )
    )

    run_id = now.strftime(
        "%Y%m%dT%H%M%S%z",
    )

    log_file = PROJECT_ROOT / "logs" / f"postgis_analysis_{run_id}.log"

    log_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    logging.basicConfig(
        level=logging.INFO,
        format=("%(asctime)s " "%(levelname)s " "%(name)s - " "%(message)s"),
        handlers=[
            logging.StreamHandler(
                sys.stdout,
            ),
            logging.FileHandler(
                log_file,
                encoding="utf-8",
            ),
        ],
    )


def find_analysis_sql_files() -> list[Path]:
    """
    Find all analysis-layer SQL files in filename order.

    Numeric filename prefixes determine execution order.
    """

    if not ANALYSIS_SQL_DIRECTORY.exists():
        raise FileNotFoundError(
            "Analysis SQL directory does not exist: " f"{ANALYSIS_SQL_DIRECTORY}"
        )

    sql_files = sorted(ANALYSIS_SQL_DIRECTORY.glob("*.sql"))

    if not sql_files:
        raise FileNotFoundError(
            "No SQL files were found in: " f"{ANALYSIS_SQL_DIRECTORY}"
        )

    return sql_files


def run_sql_file(
    *,
    engine: Engine,
    sql_file: Path,
) -> None:
    """
    Execute one standalone SQL file in one transaction.

    Python controls the transaction.
    """

    sql = sql_file.read_text(
        encoding="utf-8",
    ).strip()

    if not sql:
        raise ValueError(f"SQL file is empty: {sql_file}")

    LOGGER.info(
        "Starting SQL file: %s",
        sql_file.name,
    )

    with engine.begin() as connection:
        # Use the raw DBAPI cursor because the standalone SQL
        # files may contain PL/pgSQL "%" placeholders in
        # RAISE statements.
        raw_connection = connection.connection

        with raw_connection.cursor() as cursor:
            cursor.execute(sql)

    LOGGER.info(
        "Completed SQL file: %s",
        sql_file.name,
    )


def main() -> int:
    """
    Execute all analysis-layer SQL files in filename order.
    """

    load_dotenv(PROJECT_ROOT / ".env")

    configure_logging()

    sql_files = find_analysis_sql_files()

    LOGGER.info(
        "Analysis SQL directory: %s",
        ANALYSIS_SQL_DIRECTORY,
    )

    LOGGER.info(
        "Analysis SQL files to execute: %s",
        [sql_file.name for sql_file in sql_files],
    )

    engine = create_postgis_engine()

    try:
        test_database_connection(
            engine,
        )

        for sql_file in sql_files:
            run_sql_file(
                engine=engine,
                sql_file=sql_file,
            )

        LOGGER.info("All analysis-layer SQL files " "completed successfully.")

        return 0

    except Exception:
        LOGGER.exception("Analysis-layer SQL execution failed.")

        return 1

    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
