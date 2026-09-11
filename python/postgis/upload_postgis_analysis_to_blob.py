from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import shutil
import sys

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extensions import connection as Psycopg2Connection

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import (
    BlobServiceClient,
    ContentSettings,
)
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "data" / "processed" / "postgis_analysis"

LOGGER = logging.getLogger("upload_postgis_analysis_to_blob")

SOURCE_SCHEMA = "analysis"


# Analysis-table export configuration.
#
# Geometry columns are intentionally excluded because spatial geometry
# remains in PostGIS and is not required in the Snowflake analytical layer.
#
# order_by is used to make each CSV export deterministic. This is important
# because SHA-256 is calculated from the CSV content. The same database
# content should therefore produce the same CSV ordering and checksum.
ANALYSIS_TABLE_CONFIG: dict[str, dict[str, Any]] = {
    "building_pipe_proximity": {
        "export_columns": [
            "building_id",
            "suburb_locality",
            "town_city",
            "territorial_authority",
            "sa1_id",
            "nearest_pipe_objectid",
            "nearest_pipe_gis_id",
            "nearest_pipe_compkey",
            "nearest_pipe_process",
            "nearest_pipe_material",
            "nearest_pipe_nom_dia_mm",
            "nearest_pipe_distance_m",
            "analysis_created_at",
        ],
        "order_by": "building_id",
    },
    "sa1_dwelling_density": {
        "export_columns": [
            "sa1_id",
            "landwater",
            "landwater_name",
            "land_area_sq_km",
            "occupied_dwellings_2023",
            "dwelling_density_per_sq_km",
            "analysis_created_at",
        ],
        "order_by": "sa1_id",
    },
}


# Environment helpers
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


# Logging
def configure_logging(
    run_id: str,
) -> None:
    """
    Configure logging to the console and a timestamped log file.
    """

    log_directory = PROJECT_ROOT / "logs"

    log_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    log_file = log_directory / f"postgis_analysis_upload_{run_id}.log"

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


# Command-line arguments
def parse_arguments() -> argparse.Namespace:
    """
    Parse command-line arguments.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Export configured PostGIS analysis tables "
            "and upload changed analytical results "
            "to Azure Blob Storage."
        )
    )

    parser.add_argument(
        "--tables",
        nargs="*",
        choices=sorted(ANALYSIS_TABLE_CONFIG.keys()),
        help=(
            "Analysis tables to export. "
            "Default: export all configured analysis tables."
        ),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
        help=("Temporary local directory used for " "PostGIS analysis exports."),
    )

    parser.add_argument(
        "--keep-files",
        action="store_true",
        help=("Keep local CSV and manifest files " "after a successful Azure upload."),
    )

    return parser.parse_args()


# PostgreSQL connection
def create_postgis_connection() -> Psycopg2Connection:
    """
    Create a direct psycopg2 connection to PostgreSQL/PostGIS.
    """

    host = get_required_environment_variable("POSTGIS_HOST")

    port = os.getenv(
        "POSTGIS_PORT",
        "5432",
    )

    database = get_required_environment_variable("POSTGIS_DATABASE")

    user = get_required_environment_variable("POSTGIS_USER")

    password = get_required_environment_variable("POSTGIS_PASSWORD")

    sslmode = os.getenv(
        "POSTGIS_SSLMODE",
        "prefer",
    )

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        sslmode=sslmode,
    )


# Source-table existence validation
def validate_source_table_exists(
    connection,
    source_table: str,
) -> None:
    """
    Confirm that a configured PostGIS analysis table exists.
    """

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT to_regclass(%s);
            """,
            (f"{SOURCE_SCHEMA}.{source_table}",),
        )

        table_name = cursor.fetchone()[0]

    if table_name is None:
        raise RuntimeError(
            "PostGIS analysis table does not exist: " f"{SOURCE_SCHEMA}.{source_table}"
        )


# Building-pipe proximity validation
def validate_building_pipe_proximity(
    connection,
) -> dict[str, int | float | None]:
    """
    Validate analysis.building_pipe_proximity before export.

    Returns summary statistics used in the export manifest.
    """

    source_table = "building_pipe_proximity"

    validate_source_table_exists(
        connection,
        source_table,
    )

    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT
                COUNT(*) AS row_count,

                COUNT(*) - COUNT(DISTINCT building_id)
                    AS duplicate_building_count,

                COUNT(*) FILTER (
                    WHERE nearest_pipe_distance_m IS NULL
                       OR nearest_pipe_distance_m < 0
                ) AS invalid_distance_count,

                COUNT(*) FILTER (
                    WHERE nearest_pipe_objectid IS NULL
                ) AS missing_pipe_count,

                COUNT(*) FILTER (
                    WHERE sa1_id IS NULL
                ) AS missing_sa1_count,

                MIN(nearest_pipe_distance_m)
                    AS minimum_distance_m,

                MAX(nearest_pipe_distance_m)
                    AS maximum_distance_m

            FROM {SOURCE_SCHEMA}.{source_table};
            """)

        (
            row_count,
            duplicate_building_count,
            invalid_distance_count,
            missing_pipe_count,
            missing_sa1_count,
            minimum_distance_m,
            maximum_distance_m,
        ) = cursor.fetchone()

    if row_count == 0:
        raise RuntimeError(f"{SOURCE_SCHEMA}.{source_table} " "contains no rows.")

    if duplicate_building_count != 0:
        raise RuntimeError(
            "Duplicate building IDs found in the "
            "building-pipe proximity result. "
            f"Count={duplicate_building_count}"
        )

    if invalid_distance_count != 0:
        raise RuntimeError(
            "NULL or negative building-to-pipe "
            "distances found in the analysis result. "
            f"Count={invalid_distance_count}"
        )

    if missing_pipe_count != 0:
        raise RuntimeError(
            "Buildings without a nearest eligible "
            "pipe found in the analysis result. "
            f"Count={missing_pipe_count}"
        )

    LOGGER.info(
        "Validated PostGIS source table: %s.%s",
        SOURCE_SCHEMA,
        source_table,
    )

    LOGGER.info(
        "Source rows: %s",
        row_count,
    )

    LOGGER.info(
        "Buildings without an assigned SA1: %s",
        missing_sa1_count,
    )

    return {
        "row_count": int(row_count),
        "missing_sa1_count": int(missing_sa1_count),
        "minimum_distance_m": (
            float(minimum_distance_m) if minimum_distance_m is not None else None
        ),
        "maximum_distance_m": (
            float(maximum_distance_m) if maximum_distance_m is not None else None
        ),
    }


# SA1 dwelling-density validation
def validate_sa1_dwelling_density(
    connection,
) -> dict[str, int | float | None]:
    """
    Validate analysis.sa1_dwelling_density before export.

    NULL dwelling density is allowed because the corresponding
    Census occupied-dwelling count may be unavailable.

    Returns summary statistics used in the export manifest.
    """

    source_table = "sa1_dwelling_density"

    validate_source_table_exists(
        connection,
        source_table,
    )

    with connection.cursor() as cursor:
        cursor.execute(f"""
            SELECT
                COUNT(*) AS row_count,

                COUNT(*) - COUNT(DISTINCT sa1_id)
                    AS duplicate_sa1_count,

                COUNT(*) FILTER (
                    WHERE land_area_sq_km IS NULL
                       OR land_area_sq_km <= 0
                ) AS invalid_land_area_count,

                COUNT(*) FILTER (
                    WHERE dwelling_density_per_sq_km < 0
                ) AS invalid_density_count,

                COUNT(*) FILTER (
                    WHERE dwelling_density_per_sq_km IS NULL
                ) AS missing_density_count,

                MIN(dwelling_density_per_sq_km)
                    AS minimum_density_per_sq_km,

                MAX(dwelling_density_per_sq_km)
                    AS maximum_density_per_sq_km

            FROM {SOURCE_SCHEMA}.{source_table};
            """)

        (
            row_count,
            duplicate_sa1_count,
            invalid_land_area_count,
            invalid_density_count,
            missing_density_count,
            minimum_density_per_sq_km,
            maximum_density_per_sq_km,
        ) = cursor.fetchone()

    if row_count == 0:
        raise RuntimeError(f"{SOURCE_SCHEMA}.{source_table} " "contains no rows.")

    if duplicate_sa1_count != 0:
        raise RuntimeError(
            "Duplicate SA1 IDs found in the "
            "dwelling-density result. "
            f"Count={duplicate_sa1_count}"
        )

    if invalid_land_area_count != 0:
        raise RuntimeError(
            "NULL, zero, or negative SA1 land-area "
            "values found in the dwelling-density result. "
            f"Count={invalid_land_area_count}"
        )

    if invalid_density_count != 0:
        raise RuntimeError(
            "Negative dwelling-density values found. " f"Count={invalid_density_count}"
        )

    LOGGER.info(
        "Validated PostGIS source table: %s.%s",
        SOURCE_SCHEMA,
        source_table,
    )

    LOGGER.info(
        "Source rows: %s",
        row_count,
    )

    LOGGER.info(
        "SA1 records with unavailable " "dwelling density: %s",
        missing_density_count,
    )

    return {
        "row_count": int(row_count),
        "missing_density_count": int(missing_density_count),
        "minimum_density_per_sq_km": (
            float(minimum_density_per_sq_km)
            if minimum_density_per_sq_km is not None
            else None
        ),
        "maximum_density_per_sq_km": (
            float(maximum_density_per_sq_km)
            if maximum_density_per_sq_km is not None
            else None
        ),
    }


# Validation dispatcher
def validate_source_table(
    connection,
    source_table: str,
) -> dict[str, int | float | None]:
    """
    Run table-specific validation before export.
    """

    if source_table == "building_pipe_proximity":
        return validate_building_pipe_proximity(
            connection,
        )

    if source_table == "sa1_dwelling_density":
        return validate_sa1_dwelling_density(
            connection,
        )

    raise ValueError(
        "No validation rule configured for " f"analysis table: {source_table}"
    )


# CSV export
def export_postgis_analysis_to_csv(
    connection,
    output_path: Path,
    source_table: str,
    export_columns: list[str],
    order_by: str,
) -> Path:
    """
    Export one configured PostGIS analysis table directly to CSV.

    PostgreSQL COPY is used instead of loading the complete
    table into Python memory.

    Rows are explicitly ordered to make the generated CSV
    deterministic for SHA-256 comparison.
    """

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    selected_columns = ", ".join(export_columns)

    copy_sql = f"""
        COPY (
            SELECT
                {selected_columns}
            FROM {SOURCE_SCHEMA}.{source_table}
            ORDER BY {order_by}
        )
        TO STDOUT
        WITH (
            FORMAT CSV,
            HEADER TRUE,
            ENCODING 'UTF8'
        );
    """

    LOGGER.info(
        "Exporting %s.%s to %s",
        SOURCE_SCHEMA,
        source_table,
        output_path,
    )

    with connection.cursor() as cursor:
        with output_path.open(
            "w",
            encoding="utf-8",
            newline="",
        ) as destination:
            cursor.copy_expert(
                copy_sql,
                destination,
            )

    if not output_path.exists():
        raise RuntimeError("CSV export was not created: " f"{output_path}")

    if output_path.stat().st_size == 0:
        raise RuntimeError(f"CSV export is empty: {output_path}")

    LOGGER.info(
        "CSV export completed: %s bytes",
        output_path.stat().st_size,
    )

    return output_path


# Local CSV validation
def validate_exported_csv(
    csv_path: Path,
    expected_row_count: int,
    export_columns: list[str],
) -> None:
    """
    Validate the exported CSV header and row count.
    """

    actual_row_count = 0

    with csv_path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as source:
        reader = csv.reader(
            source,
        )

        try:
            header = next(reader)

        except StopIteration as exc:
            raise RuntimeError(f"CSV file is empty: {csv_path}") from exc

        if header != export_columns:
            raise RuntimeError(
                "CSV column mismatch.\n"
                f"Expected: {export_columns}\n"
                f"Actual:   {header}"
            )

        for _ in reader:
            actual_row_count += 1

    if actual_row_count != expected_row_count:
        raise RuntimeError(
            "CSV row-count validation failed. "
            f"PostGIS={expected_row_count}, "
            f"CSV={actual_row_count}"
        )

    LOGGER.info(
        "CSV validation succeeded. Rows=%s",
        actual_row_count,
    )


# SHA-256
def sha256_file(
    path: Path,
    chunk_size: int = 1024 * 1024,
) -> str:
    """
    Calculate the SHA-256 checksum of a file.
    """

    digest = hashlib.sha256()

    with path.open(
        "rb",
    ) as source:
        for chunk in iter(
            lambda: source.read(chunk_size),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


# Manifest
def write_manifest(
    *,
    manifest_path: Path,
    csv_path: Path,
    source_table: str,
    export_columns: list[str],
    run_id: str,
    exported_at: str,
    source_statistics: dict[
        str,
        int | float | None,
    ],
    csv_sha256: str,
) -> Path:
    """
    Write metadata describing one PostGIS analysis export.
    """

    quality_statistics = {
        key: value for key, value in source_statistics.items() if key != "row_count"
    }

    manifest = {
        "run_id": run_id,
        "exported_at": exported_at,
        "source": {
            "database": (get_required_environment_variable("POSTGIS_DATABASE")),
            "schema": SOURCE_SCHEMA,
            "table": source_table,
        },
        "export": {
            "filename": csv_path.name,
            "format": "csv",
            "encoding": "utf-8",
            "columns": export_columns,
            "row_count": (source_statistics["row_count"]),
            "size_bytes": (csv_path.stat().st_size),
            "sha256": csv_sha256,
        },
        "quality": quality_statistics,
        "geometry_exported": False,
        "notes": (
            "PostGIS geometry columns are "
            "intentionally excluded from the "
            "analytical CSV export."
        ),
    }

    manifest_path.write_text(
        json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    LOGGER.info(
        "Manifest created: %s",
        manifest_path,
    )

    return manifest_path


# Azure Blob Storage
def get_analysis_container_client():
    """
    Create the Azure Blob container client for processed
    PostGIS analysis outputs.

    The same Azure Storage account is used as the raw
    ingestion pipeline, but a different container is selected.
    """

    connection_string = get_required_environment_variable(
        "AZURE_STORAGE_CONNECTION_STRING"
    )

    container_name = os.getenv(
        "AZURE_STORAGE_ANALYSIS_CONTAINER_NAME",
        "processed",
    ).strip()

    if not container_name:
        raise EnvironmentError(
            "AZURE_STORAGE_ANALYSIS_CONTAINER_NAME " "cannot be empty."
        )

    blob_service_client = BlobServiceClient.from_connection_string(connection_string)

    container_client = blob_service_client.get_container_client(container_name)

    try:
        container_client.create_container()

        LOGGER.info(
            "Created Azure Blob container: %s",
            container_name,
        )

    except ResourceExistsError:
        LOGGER.info(
            "Using existing Azure Blob container: %s",
            container_name,
        )

    return container_client


def find_latest_manifest_blob(
    container_client,
    source_table: str,
) -> str | None:
    """
    Find the latest uploaded manifest for one
    PostGIS analysis table in Azure.
    """

    prefix = f"postgis_analysis/{source_table}/"

    manifest_blob_names: list[str] = []

    for blob in container_client.list_blobs(
        name_starts_with=prefix,
    ):
        if blob.name.endswith("/manifest.json"):
            manifest_blob_names.append(blob.name)

    if not manifest_blob_names:
        return None

    manifest_blob_names.sort()

    return manifest_blob_names[-1]


def read_manifest_sha256(
    container_client,
    manifest_blob_name: str,
) -> str:
    """
    Read the CSV SHA-256 value from an Azure manifest.
    """

    blob_client = container_client.get_blob_client(manifest_blob_name)

    manifest_bytes = blob_client.download_blob().readall()

    manifest = json.loads(manifest_bytes.decode("utf-8"))

    try:
        return str(manifest["export"]["sha256"])

    except (
        KeyError,
        TypeError,
    ) as exc:
        raise ValueError(
            "Manifest does not contain " "export.sha256: " f"{manifest_blob_name}"
        ) from exc


def is_export_unchanged(
    *,
    container_client,
    source_table: str,
    current_sha256: str,
) -> bool:
    """
    Check whether the current CSV matches the latest
    Azure export for the same analysis table.
    """

    latest_manifest_blob = find_latest_manifest_blob(
        container_client,
        source_table,
    )

    if latest_manifest_blob is None:
        LOGGER.info(
            "No previous analysis manifest " "found for %s.",
            source_table,
        )

        return False

    previous_sha256 = read_manifest_sha256(
        container_client,
        latest_manifest_blob,
    )

    if current_sha256 == previous_sha256:
        LOGGER.info(
            "%s export is unchanged. " "SHA-256=%s",
            source_table,
            current_sha256,
        )

        return True

    LOGGER.info(
        "%s export has changed. " "Previous SHA-256=%s " "Current SHA-256=%s",
        source_table,
        previous_sha256,
        current_sha256,
    )

    return False


# Azure upload
def upload_export_files(
    *,
    container_client,
    source_table: str,
    run_directory: Path,
    export_date: str,
    run_id: str,
) -> list[str]:
    """
    Upload one analysis table's CSV and manifest
    to Azure Blob Storage.

    Blob structure:

    postgis_analysis/
        <source_table>/
            export_date=YYYY-MM-DD/
                <run_id>/
                    <source_table>.csv
                    manifest.json
    """

    blob_prefix = (
        "postgis_analysis/" f"{source_table}/" f"export_date={export_date}/" f"{run_id}"
    )

    local_files = sorted(path for path in run_directory.iterdir() if path.is_file())

    if not local_files:
        raise RuntimeError("No files found for upload: " f"{run_directory}")

    uploaded_blob_names: list[str] = []

    for file_path in local_files:
        blob_name = f"{blob_prefix}/" f"{file_path.name}"

        if file_path.suffix.lower() == ".csv":
            content_type = "text/csv"

        elif file_path.suffix.lower() == ".json":
            content_type = "application/json"

        else:
            content_type = "application/octet-stream"

        LOGGER.info(
            "Uploading %s",
            blob_name,
        )

        blob_client = container_client.get_blob_client(blob_name)

        with file_path.open(
            "rb",
        ) as source:
            blob_client.upload_blob(
                data=source,
                overwrite=False,
                content_settings=ContentSettings(
                    content_type=content_type,
                ),
            )

        uploaded_blob_names.append(blob_name)

    return uploaded_blob_names


# Process one configured analysis table
def process_analysis_table(
    *,
    connection,
    container_client,
    source_table: str,
    table_config: dict[str, Any],
    output_directory: Path,
    export_date: str,
    run_id: str,
    exported_at: str,
    keep_files: bool,
) -> None:
    """
    Validate, export, checksum and upload
    one configured PostGIS analysis table.
    """

    export_columns = table_config["export_columns"]

    order_by = table_config["order_by"]

    run_directory = (
        output_directory / source_table / f"export_date={export_date}" / run_id
    )

    csv_path = run_directory / f"{source_table}.csv"

    manifest_path = run_directory / "manifest.json"

    LOGGER.info(
        "Starting analysis export: %s.%s",
        SOURCE_SCHEMA,
        source_table,
    )

    # Validate the PostGIS analysis result.
    source_statistics = validate_source_table(
        connection,
        source_table,
    )

    # Export analytical attributes to CSV.
    export_postgis_analysis_to_csv(
        connection,
        csv_path,
        source_table,
        export_columns,
        order_by,
    )

    # Validate the generated CSV.
    validate_exported_csv(
        csv_path,
        expected_row_count=int(source_statistics["row_count"]),
        export_columns=export_columns,
    )

    # Calculate deterministic CSV checksum.
    current_sha256 = sha256_file(csv_path)

    # Skip Azure upload when the exported
    # analytical content has not changed.
    if is_export_unchanged(
        container_client=container_client,
        source_table=source_table,
        current_sha256=current_sha256,
    ):
        LOGGER.info(
            "Skipping Azure upload for %s "
            "because the CSV content "
            "has not changed.",
            source_table,
        )

        if not keep_files:
            shutil.rmtree(run_directory)

            LOGGER.info(
                "Removed unchanged local " "export directory: %s",
                run_directory,
            )

        return

    # Create metadata only for a new export.
    write_manifest(
        manifest_path=manifest_path,
        csv_path=csv_path,
        source_table=source_table,
        export_columns=export_columns,
        run_id=run_id,
        exported_at=exported_at,
        source_statistics=source_statistics,
        csv_sha256=current_sha256,
    )

    # Upload the new CSV and manifest.
    uploaded_blob_names = upload_export_files(
        container_client=container_client,
        source_table=source_table,
        run_directory=run_directory,
        export_date=export_date,
        run_id=run_id,
    )

    LOGGER.info(
        "Analysis upload completed " "successfully for %s.",
        source_table,
    )

    for blob_name in uploaded_blob_names:
        LOGGER.info(
            "Uploaded blob: %s",
            blob_name,
        )

    # Remove temporary local files after
    # successful upload unless explicitly retained.
    if not keep_files:
        shutil.rmtree(run_directory)

        LOGGER.info(
            "Removed local export directory: %s",
            run_directory,
        )


# Main
def main() -> int:
    """
    Export configured PostGIS analysis tables
    and upload changed results to Azure Blob Storage.
    """

    load_dotenv(PROJECT_ROOT / ".env")

    arguments = parse_arguments()

    now = datetime.now(ZoneInfo("Pacific/Auckland"))

    run_id = now.strftime("%Y%m%dT%H%M%S%z")

    export_date = now.date().isoformat()

    exported_at = now.isoformat()

    configure_logging(run_id)

    # If --tables is not provided, process
    # every configured analysis table.
    if arguments.tables:
        source_tables = arguments.tables

    else:
        source_tables = list(ANALYSIS_TABLE_CONFIG.keys())

    LOGGER.info(
        "Analysis tables selected " "for export: %s",
        source_tables,
    )

    connection = None

    try:
        LOGGER.info("Connecting to PostGIS.")

        connection = create_postgis_connection()

        # This script only reads existing
        # PostGIS analysis results.
        connection.set_session(
            readonly=True,
            autocommit=False,
        )

        # Create one Azure container client
        # and reuse it for every analysis table.
        container_client = get_analysis_container_client()

        for source_table in source_tables:
            table_config = ANALYSIS_TABLE_CONFIG[source_table]

            process_analysis_table(
                connection=connection,
                container_client=container_client,
                source_table=source_table,
                table_config=table_config,
                output_directory=(arguments.output_dir),
                export_date=export_date,
                run_id=run_id,
                exported_at=exported_at,
                keep_files=(arguments.keep_files),
            )

        LOGGER.info("All selected PostGIS analysis " "exports completed successfully.")

        return 0

    except Exception:
        LOGGER.exception("PostGIS analysis " "export/upload failed.")

        return 1

    finally:
        if connection is not None:
            connection.close()


# Entry point
if __name__ == "__main__":
    raise SystemExit(main())
