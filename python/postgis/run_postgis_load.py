from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import geopandas as gpd
import pandas as pd

from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Integer,
    SmallInteger,
    String,
    create_engine,
    text,
)
from sqlalchemy.engine import Engine

from db import (
    create_postgis_engine,
    get_required_environment_variable,
    test_database_connection,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_STAGING_DIRECTORY = PROJECT_ROOT / "data" / "staging"

DEFAULT_TARGET_CRS = "EPSG:2193"

ARCGIS_TO_PANDAS_DTYPE = {
    "esriFieldTypeSmallInteger": "Int16",
    "esriFieldTypeInteger": "Int32",
    "esriFieldTypeOID": "Int64",
    "esriFieldTypeDouble": "Float64",
    "esriFieldTypeString": "string",
}

ARCGIS_DATE_FIELD_TYPES = {
    "esriFieldTypeDate",
}

# Mapping Schema
# ArcGIS                     Pandas            PostgreSQL
# ------------------------------------------------------------
# SmallInteger               Int16             SMALLINT
# Integer                    Int32             INTEGER
# OID                        Int64             BIGINT
# Double                     Float64           DOUBLE PRECISION
# String                     string            VARCHAR(n)
# Date                       datetime          TIMESTAMPTZ

LOGGER = logging.getLogger("run_postgis_load")


DATASET_PROVIDER_MAP = {
    "nz_building_outlines": "linz",
    "urban_rural_2026": "stats_nz",
    "census_2023_dwellings_sa1": "stats_nz",
    "water_pipe": "watercare",
}


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

    log_file = PROJECT_ROOT / "logs" / f"postgis_load_{run_id}.log"

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


def parse_arguments() -> argparse.Namespace:
    """
    Read command-line arguments.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Download the latest raw spatial datasets "
            "from Azure Blob Storage, validate them, "
            "transform them to EPSG:2193 and load them "
            "into PostGIS."
        )
    )

    # Optional list of dataset short names to process.
    # If omitted, all datasets defined in DATASET_PROVIDER_MAP are loaded.
    parser.add_argument(
        "--datasets",
        nargs="*",
        choices=sorted(DATASET_PROVIDER_MAP.keys()),
        help=("Dataset short names to load. " "Default: load all configured datasets."),
    )

    # Target PostGIS schema name.
    # If omitted, use POSTGIS_TARGET_SCHEMA from the environment,
    # falling back to "staging" when that variable is not set.
    parser.add_argument(
        "--schema",
        default=None,
        help=("Target PostGIS schema. " "Default: POSTGIS_TARGET_SCHEMA or staging."),
    )

    # Local root directory used to temporarily store files downloaded
    # from Azure Blob Storage before they are loaded into PostGIS.
    parser.add_argument(
        "--staging-dir",
        type=Path,
        default=DEFAULT_STAGING_DIRECTORY,
        help=("Local temporary directory used for " "files downloaded from Azure."),
    )

    parser.add_argument(
        # Boolean flag: False by default. When provided, keep the downloaded
        # manifest and page files for each dataset run after a successful load from Azure Blob Storage.
        "--keep-files",
        action="store_true",
        help=(
            "Keep the downloaded manifest and page files for each dataset run "
            "after a successful database load. By default, the run-specific "
            "staging directory is deleted after the load succeeds."
        ),
    )

    return parser.parse_args()


def validate_identifier(
    value: str,
) -> None:
    """
    Validate PostgreSQL identifiers.
    """

    # Allow a lowercase letter or "_" first,
    # followed by zero or more lowercase letters, digits, or "_".
    if not re.fullmatch(
        r"[a-z_][a-z0-9_]*",
        value,
    ):
        raise ValueError(f"Unsafe PostgreSQL identifier: " f"{value!r}")


def create_target_schema(
    engine: Engine,
    schema_name: str,
) -> None:
    """
    Create the target schema if it does not already exist.
    """

    validate_identifier(
        schema_name,
    )

    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}"))


def get_container_client():
    """
    Create an Azure Blob Storage container client.
    """

    connection_string = get_required_environment_variable(
        "AZURE_STORAGE_CONNECTION_STRING",
    )

    container_name = os.getenv(
        "AZURE_STORAGE_RAW_CONTAINER_NAME=raw",
        "raw",
    )

    blob_service_client = BlobServiceClient.from_connection_string(
        connection_string,
    )

    return blob_service_client.get_container_client(
        container_name,
    )


def normalize_blob_name(
    blob_name: str,
) -> str:
    """
    Normalize Blob path separators to forward slashes.
    """

    return blob_name.replace(
        "\\",
        "/",
    )


def find_latest_manifest_blob(
    *,
    container_client,
    provider: str,
    dataset_short_name: str,
) -> str:
    """
    Find the latest manifest.json for a dataset in Azure Blob Storage.

    Expected Blob path structure:

    provider/
    dataset/
    ingestion_date=YYYY-MM-DD/
    run_id/
    manifest.json
    """

    prefix = f"{provider}/" f"{dataset_short_name}/"

    manifest_blob_names: list[str] = []

    for blob in container_client.list_blobs(
        name_starts_with=prefix,
    ):
        blob_name = normalize_blob_name(
            blob.name,
        )

        if blob_name.endswith("/manifest.json"):
            manifest_blob_names.append(blob_name)

    if not manifest_blob_names:
        raise FileNotFoundError(
            f"No manifest.json was found in Azure "
            f"for dataset={dataset_short_name}, "
            f"prefix={prefix}"
        )

    # The date and run_id formats sort chronologically as strings,
    # so the final sorted path represents the latest run.
    manifest_blob_names.sort()

    return manifest_blob_names[-1]


def download_blob_to_file(
    *,
    container_client,
    blob_name: str,
    destination_path: Path,
) -> Path:
    """
    Download a single Azure Blob to a local file.
    """

    destination_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    blob_client = container_client.get_blob_client(
        blob_name,
    )

    with destination_path.open(
        "wb",
    ) as destination:
        stream = blob_client.download_blob()

        stream.readinto(
            destination,
        )

    return destination_path


def read_manifest(
    manifest_path: Path,
) -> dict[str, Any]:
    """
    Read and validate the basic structure of manifest.json.
    """

    try:
        manifest = json.loads(
            manifest_path.read_text(
                encoding="utf-8",
            )
        )

    except json.JSONDecodeError as exc:
        raise ValueError(f"Manifest is not valid JSON: " f"{manifest_path}") from exc

    if not isinstance(
        manifest,
        dict,
    ):
        raise ValueError(f"Manifest must contain a JSON object: " f"{manifest_path}")

    required_fields = {
        "run_id",
        "dataset_name",
        "short_name",
        "provider",
        "source_type",
        "downloaded_count",
        "page_count",
        "pages",
    }

    missing_fields = required_fields - manifest.keys()

    if missing_fields:
        raise ValueError(
            f"Manifest is missing required fields: " f"{sorted(missing_fields)}"
        )

    pages = manifest["pages"]

    if not isinstance(
        pages,
        list,
    ):
        raise ValueError("Manifest 'pages' must be a list.")

    if len(pages) != int(manifest["page_count"]):
        raise ValueError("Manifest page_count does not match " "the pages list length.")

    return manifest


def sha256_file(
    path: Path,
    chunk_size: int = 1024 * 1024,
) -> str:
    """
    Calculate the SHA-256 checksum of a local file.
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


def validate_downloaded_file(
    *,
    page_path: Path,
    page_metadata: dict[str, Any],
) -> None:
    """
    Validate a downloaded file against manifest size and SHA-256 metadata.
    """

    if not page_path.exists():
        raise FileNotFoundError(f"Downloaded page file does not exist: " f"{page_path}")

    expected_size = int(page_metadata["size_bytes"])

    actual_size = page_path.stat().st_size

    if actual_size != expected_size:
        raise ValueError(
            f"File size mismatch for "
            f"{page_path.name}. "
            f"Expected={expected_size}, "
            f"actual={actual_size}"
        )

    # Normalize defensively: metadata may come from external sources and hex case may vary.
    expected_sha256 = str(page_metadata["sha256"]).lower()

    actual_sha256 = sha256_file(
        page_path,
    ).lower()

    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"SHA-256 mismatch for "
            f"{page_path.name}. "
            f"Expected={expected_sha256}, "
            f"actual={actual_sha256}"
        )


def resolve_source_crs(
    manifest: dict[str, Any],
) -> str | None:
    """
    Read the source CRS from the manifest.
    """

    source_configuration = manifest.get("source_configuration")

    if not isinstance(source_configuration, dict):
        return None

    value = source_configuration.get("srs_name")

    return str(value) if value else None


def download_dataset_run(
    *,
    container_client,
    provider: str,
    dataset_short_name: str,
    staging_root: Path,
) -> tuple[
    dict[str, Any],
    Path,
    list[Path],
]:
    """
    Download the latest manifest and page files for a dataset run.
    """

    manifest_blob_name = find_latest_manifest_blob(
        container_client=container_client,
        provider=provider,
        dataset_short_name=dataset_short_name,
    )

    # Remove "/manifest.json" to get the Blob prefix for this ingestion run.
    run_blob_prefix = manifest_blob_name.rsplit(
        "/",
        1,
    )[0]

    temporary_manifest_path = staging_root / dataset_short_name / "manifest.json"

    download_blob_to_file(
        container_client=container_client,
        blob_name=manifest_blob_name,
        destination_path=temporary_manifest_path,
    )

    manifest = read_manifest(
        temporary_manifest_path,
    )

    if manifest["short_name"] != dataset_short_name:
        raise ValueError(
            f"Manifest short_name mismatch. "
            f"Expected={dataset_short_name}, "
            f"actual={manifest['short_name']}"
        )

    run_id = str(manifest["run_id"])

    dataset_staging_directory = staging_root / dataset_short_name / run_id

    if dataset_staging_directory.exists():
        shutil.rmtree(dataset_staging_directory)

    pages_directory = dataset_staging_directory / "pages"

    final_manifest_path = dataset_staging_directory / "manifest.json"

    final_manifest_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.move(
        str(temporary_manifest_path),
        str(final_manifest_path),
    )

    page_paths: list[Path] = []

    for page_metadata in manifest["pages"]:
        filename = page_metadata.get("filename")

        if not filename:
            raise ValueError("A manifest page does not contain " "a filename.")

        filename = Path(str(filename)).name

        page_blob_name = f"{run_blob_prefix}/" f"pages/" f"{filename}"

        page_path = pages_directory / filename

        LOGGER.info(
            "Downloading blob: %s",
            page_blob_name,
        )

        download_blob_to_file(
            container_client=container_client,
            blob_name=page_blob_name,
            destination_path=page_path,
        )

        validate_downloaded_file(
            page_path=page_path,
            page_metadata=page_metadata,
        )

        page_paths.append(page_path)

    LOGGER.info(
        "%s downloaded and verified: " "run_id=%s pages=%s",
        dataset_short_name,
        run_id,
        len(page_paths),
    )

    return (
        manifest,
        dataset_staging_directory,
        page_paths,
    )


def read_geojson_page(
    *,
    page_path: Path,
    manifest_source_crs: str | None,
) -> gpd.GeoDataFrame:
    """
    Read a GeoJSON page and ensure that its CRS is available.
    """

    gdf = gpd.read_file(
        page_path,
        engine="pyogrio",
    )

    if not isinstance(
        gdf,
        gpd.GeoDataFrame,
    ):
        raise TypeError(f"File did not produce a GeoDataFrame: " f"{page_path}")

    if "geometry" not in gdf.columns:
        raise ValueError(f"GeoJSON does not contain a geometry " f"column: {page_path}")

    if gdf.crs is None:
        if not manifest_source_crs:
            raise ValueError(
                f"CRS is missing from both the " f"GeoJSON and manifest: {page_path}"
            )

        LOGGER.warning(
            "%s does not expose a CRS through GDAL. "
            "Assigning manifest source CRS=%s",
            page_path.name,
            manifest_source_crs,
        )

        gdf = gdf.set_crs(
            manifest_source_crs,
            allow_override=True,
        )

    return gdf


def validate_geometry(
    *,
    gdf: gpd.GeoDataFrame,
    dataset_short_name: str,
    page_path: Path,
) -> gpd.GeoDataFrame:
    """
    Validate geometry values before loading data into PostGIS.

    Rows with missing or empty geometries are removed.
    Invalid geometries are not repaired automatically; instead,
    the load is stopped so the source data can be investigated.
    """

    # Nothing to validate if the page contains no features.
    if gdf.empty:
        LOGGER.warning(
            "%s is empty: %s",
            dataset_short_name,
            page_path.name,
        )

        return gdf

    # Identify geometries that are missing (None/NA)
    # or exist but contain no coordinates.
    missing_geometry_mask = gdf.geometry.isna()

    empty_geometry_mask = gdf.geometry.is_empty

    missing_count = int(missing_geometry_mask.sum())

    empty_count = int(empty_geometry_mask.sum())

    if missing_count > 0 or empty_count > 0:
        LOGGER.warning(
            "%s page=%s contains "
            "missing_geometry=%s "
            "empty_geometry=%s. "
            "These rows will be removed.",
            dataset_short_name,
            page_path.name,
            missing_count,
            empty_count,
        )

        # Keep only rows whose geometry is neither missing nor empty.
        valid_presence_mask = ~(missing_geometry_mask | empty_geometry_mask)

        gdf = gdf.loc[valid_presence_mask].copy()

    # All rows may have been removed by the previous filtering step.
    if gdf.empty:
        return gdf

    # Geometry exists, but it may still violate spatial validity rules
    # such as a self-intersecting polygon.
    invalid_mask = ~gdf.geometry.is_valid

    invalid_count = int(invalid_mask.sum())

    if invalid_count > 0:
        raise ValueError(
            f"{dataset_short_name} contains "
            f"{invalid_count} invalid geometries "
            f"in {page_path.name}. "
            "The data was not loaded into PostGIS."
        )

    return gdf


def transform_to_target_crs(
    *,
    gdf: gpd.GeoDataFrame,
    target_crs: str,
    dataset_short_name: str,
) -> gpd.GeoDataFrame:
    """
    Transform spatial data to the configured target CRS.
    """

    if gdf.empty:
        return gdf

    actual_crs = gdf.crs

    if actual_crs is None:
        raise ValueError(f"{dataset_short_name} has no CRS.")

    if not actual_crs.equals(target_crs):
        LOGGER.info(
            "%s transforming CRS from %s to %s",
            dataset_short_name,
            actual_crs,
            target_crs,
        )

        gdf = gdf.to_crs(
            target_crs,
        )

    return gdf


def normalize_column_name(
    column_name: str,
) -> str:
    """
    Normalize a column name to PostgreSQL-friendly snake_case.
    """

    normalized = column_name.strip()

    normalized = re.sub(
        r"([a-z0-9])([A-Z])",
        r"\1_\2",
        normalized,
    )

    normalized = re.sub(
        r"[^A-Za-z0-9_]+",
        "_",
        normalized,
    )

    normalized = re.sub(
        r"_+",
        "_",
        normalized,
    )

    normalized = normalized.strip("_").lower()

    if not normalized:
        normalized = "unnamed_column"

    if normalized[0].isdigit():
        normalized = f"field_{normalized}"

    return normalized


def normalize_columns(
    gdf: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """
    Normalize column names and check for duplicates.
    """

    # Build a mapping from original column names to normalized names.
    renamed_columns = {}

    for column in gdf.columns:
        if column == gdf.geometry.name:
            # Standardize the active geometry column name.
            renamed_columns[column] = "geometry"
        else:
            # Normalize non-geometry column names.
            renamed_columns[column] = normalize_column_name(str(column))

    # Collect all normalized column names.
    normalized_names = list(renamed_columns.values())

    # Find duplicate names created during normalization.
    duplicate_names = set()

    for name in normalized_names:
        if normalized_names.count(name) > 1:
            duplicate_names.add(name)

    # Stop if normalization produces duplicate column names.
    if duplicate_names:
        raise ValueError(
            "Column name normalization produced "
            f"duplicates: {sorted(duplicate_names)}"
        )

    # Rename the columns using the generated mapping.
    gdf = gdf.rename(
        columns=renamed_columns,
    )

    # Set "geometry" as the active geometry column.
    gdf = gdf.set_geometry(
        "geometry",
    )

    return gdf


def add_audit_columns(
    *,
    gdf: gpd.GeoDataFrame,
    manifest: dict[str, Any],
    page_path: Path,
    source_crs: str | None,
) -> gpd.GeoDataFrame:
    """
    Add data lineage and audit columns.
    """

    # Record the current Auckland time as the load timestamp.
    loaded_at = datetime.now(
        ZoneInfo(
            "Pacific/Auckland",
        )
    )

    gdf = gdf.copy()

    gdf["ingestion_run_id"] = str(manifest["run_id"])

    # Extract the first 10 characters of downloaded_at,
    # e.g. "2026-08-12T10:30:00+12:00" -> "2026-08-12".
    gdf["ingestion_date"] = str(
        manifest.get(
            "downloaded_at",
            "",
        )
    )[:10]

    gdf["source_provider"] = str(manifest["provider"])

    gdf["source_crs"] = source_crs

    gdf["source_file"] = page_path.name

    gdf["loaded_at"] = loaded_at

    return gdf


def read_arcgis_field_definitions(
    page_path: Path,
) -> list[dict[str, Any]]:
    """
    Read ArcGIS field definitions from an ArcGIS REST JSON page.
    """

    try:
        document = json.loads(
            page_path.read_text(
                encoding="utf-8",
            )
        )

    except json.JSONDecodeError as exc:
        raise ValueError(
            f"ArcGIS REST page is not valid JSON: " f"{page_path}"
        ) from exc

    if not isinstance(
        document,
        dict,
    ):
        raise ValueError(
            f"ArcGIS REST page must contain a JSON object: " f"{page_path}"
        )

    fields = document.get(
        "fields",
    )

    if not isinstance(
        fields,
        list,
    ):
        raise ValueError(
            f"ArcGIS REST page does not contain a valid " f"'fields' list: {page_path}"
        )

    if not fields:
        raise ValueError(
            f"ArcGIS REST page contains no field definitions: " f"{page_path}"
        )

    validated_fields: list[dict[str, Any]] = []

    for field in fields:
        if not isinstance(
            field,
            dict,
        ):
            raise ValueError(
                f"ArcGIS REST field definition is not an object: " f"{field!r}"
            )

        field_name = field.get(
            "name",
        )

        field_type = field.get(
            "type",
        )

        if not field_name or not field_type:
            raise ValueError(
                "ArcGIS REST field definition must contain "
                f"'name' and 'type': {field!r}"
            )

        validated_fields.append(
            field,
        )

    return validated_fields


def build_arcgis_postgis_dtype_mapping(
    field_definitions: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Map ArcGIS source field types to SQLAlchemy/PostgreSQL types.
    """

    dtype_mapping: dict[str, Any] = {}

    for field in field_definitions:
        source_name = str(field["name"])

        field_type = str(field["type"])

        column_name = normalize_column_name(
            source_name,
        )

        if field_type == "esriFieldTypeSmallInteger":
            dtype_mapping[column_name] = SmallInteger()

        elif field_type == "esriFieldTypeInteger":
            dtype_mapping[column_name] = Integer()

        elif field_type == "esriFieldTypeOID":
            dtype_mapping[column_name] = BigInteger()

        elif field_type == "esriFieldTypeDouble":
            dtype_mapping[column_name] = Float(
                precision=53,
            )

        elif field_type == "esriFieldTypeString":
            length = field.get(
                "length",
            )

            if length is None:
                dtype_mapping[column_name] = String()

            else:
                dtype_mapping[column_name] = String(
                    length=int(length),
                )

        elif field_type == "esriFieldTypeDate":
            dtype_mapping[column_name] = DateTime(
                timezone=True,
            )

        else:
            raise ValueError(
                f"Unsupported ArcGIS field type "
                f"{field_type!r} for field "
                f"{source_name!r}."
            )

    return dtype_mapping


def normalize_arcgis_field_types(
    *,
    gdf: gpd.GeoDataFrame,
    field_definitions: list[dict[str, Any]],
    page_path: Path,
) -> gpd.GeoDataFrame:
    """
    Convert ArcGIS attributes to deterministic Pandas dtypes.

    All source attributes are retained.
    """

    gdf = gdf.copy()

    expected_columns: set[str] = set()

    for field in field_definitions:
        source_name = str(field["name"])

        field_type = str(field["type"])

        column_name = normalize_column_name(
            source_name,
        )

        expected_columns.add(
            column_name,
        )

        # Ensure every declared ArcGIS field exists after normalization.
        if column_name not in gdf.columns:
            raise ValueError(
                f"ArcGIS field {source_name!r} "
                f"is missing from {page_path.name} "
                f"after column normalization."
            )

        # Convert numeric and string fields to deterministic Pandas dtypes.
        if field_type in ARCGIS_TO_PANDAS_DTYPE:
            pandas_dtype = ARCGIS_TO_PANDAS_DTYPE[field_type]

            if field_type in {
                "esriFieldTypeSmallInteger",
                "esriFieldTypeInteger",
                "esriFieldTypeOID",
                "esriFieldTypeDouble",
            }:
                gdf[column_name] = pd.to_numeric(
                    gdf[column_name],
                    errors="raise",
                ).astype(pandas_dtype)

            elif field_type == "esriFieldTypeString":
                gdf[column_name] = gdf[column_name].astype(pandas_dtype)

        # Handle either already-decoded datetimes or ArcGIS epoch milliseconds.
        elif field_type in ARCGIS_DATE_FIELD_TYPES:
            series = gdf[column_name]

            if pd.api.types.is_datetime64_any_dtype(series.dtype):
                gdf[column_name] = pd.to_datetime(
                    series,
                    errors="raise",
                    utc=True,
                )

            else:
                gdf[column_name] = pd.to_datetime(
                    series,
                    unit="ms",
                    errors="raise",
                    utc=True,
                )

        else:
            raise ValueError(
                f"Unsupported ArcGIS field type "
                f"{field_type!r} for field "
                f"{source_name!r}."
            )

    actual_attribute_columns = {
        column for column in gdf.columns if column != "geometry"
    }

    unexpected_columns = actual_attribute_columns - expected_columns

    if unexpected_columns:
        raise ValueError(
            f"{page_path.name} produced columns that "
            f"are not declared in the ArcGIS source schema: "
            f"{sorted(unexpected_columns)}"
        )

    return gdf


def read_arcgis_rest_page(
    *,
    page_path: Path,
    manifest_source_crs: str | None,
) -> gpd.GeoDataFrame:
    """
    Read an ArcGIS REST JSON page and ensure that its CRS is available.
    """

    gdf = gpd.read_file(
        page_path,
        engine="pyogrio",
    )

    if not isinstance(
        gdf,
        gpd.GeoDataFrame,
    ):
        raise TypeError(
            f"ArcGIS REST JSON did not produce " f"a GeoDataFrame: {page_path}"
        )

    if "geometry" not in gdf.columns:
        raise ValueError(
            f"ArcGIS REST JSON does not contain " f"a geometry column: {page_path}"
        )

    if gdf.crs is None:
        if not manifest_source_crs:
            raise ValueError(
                f"CRS is missing from both the "
                f"ArcGIS REST JSON and manifest: "
                f"{page_path}"
            )

        LOGGER.warning(
            "%s does not expose a CRS through GDAL. "
            "Assigning manifest source CRS=%s",
            page_path.name,
            manifest_source_crs,
        )

        gdf = gdf.set_crs(
            manifest_source_crs,
            allow_override=True,
        )

    return gdf


def read_and_prepare_page(
    *,
    page_path: Path,
    dataset_short_name: str,
    manifest_source_crs: str | None,
    arcgis_field_definitions: list[dict[str, Any]] | None,
) -> gpd.GeoDataFrame:
    """
    Read a source page and apply source-specific schema normalization.
    """

    if dataset_short_name == "water_pipe":
        if arcgis_field_definitions is None:
            raise ValueError(
                "ArcGIS field definitions are required " "for the water_pipe dataset."
            )

        gdf = read_arcgis_rest_page(
            page_path=page_path,
            manifest_source_crs=manifest_source_crs,
        )

        gdf = normalize_columns(
            gdf,
        )

        gdf = normalize_arcgis_field_types(
            gdf=gdf,
            field_definitions=arcgis_field_definitions,
            page_path=page_path,
        )

        return gdf

    gdf = read_geojson_page(
        page_path=page_path,
        manifest_source_crs=manifest_source_crs,
    )

    return normalize_columns(
        gdf,
    )


def prepare_target_table(
    *,
    engine: Engine,
    schema_name: str,
    table_name: str,
) -> None:
    """
    Drop the previous staging table for a full replacement load.
    """

    # Validate the schema and table names before using them in SQL.
    validate_identifier(
        schema_name,
    )

    validate_identifier(
        table_name,
    )

    # Open a transaction and drop the existing target table if it exists.
    with engine.begin() as connection:
        connection.execute(
            text(f'DROP TABLE IF EXISTS "{schema_name}"."{table_name}" CASCADE')
        )


def write_pages_to_postgis(
    *,
    engine: Engine,
    schema_name: str,
    table_name: str,
    dataset_short_name: str,
    manifest: dict[str, Any],
    page_paths: list[Path],
    target_crs: str,
) -> int:
    """
    Validate, normalize, transform, and write spatial source pages
    to PostGIS.
    """

    source_crs = resolve_source_crs(
        manifest,
    )

    expected_feature_count = int(manifest["downloaded_count"])

    source_feature_count = 0

    database_feature_count = 0

    first_written_page = True

    arcgis_field_definitions: list[dict[str, Any]] | None = None

    postgis_dtype_mapping: dict[str, Any] | None = None

    if dataset_short_name == "water_pipe":
        if not page_paths:
            raise ValueError("water_pipe contains no source pages.")

        arcgis_field_definitions = read_arcgis_field_definitions(
            page_paths[0],
        )

        postgis_dtype_mapping = build_arcgis_postgis_dtype_mapping(
            arcgis_field_definitions,
        )

        LOGGER.info(
            "%s ArcGIS source schema loaded: fields=%s",
            dataset_short_name,
            len(arcgis_field_definitions),
        )

    prepare_target_table(
        engine=engine,
        schema_name=schema_name,
        table_name=table_name,
    )

    for page_path in page_paths:
        LOGGER.info(
            "%s reading page: %s",
            dataset_short_name,
            page_path.name,
        )

        gdf = read_and_prepare_page(
            page_path=page_path,
            dataset_short_name=dataset_short_name,
            manifest_source_crs=source_crs,
            arcgis_field_definitions=(arcgis_field_definitions),
        )

        source_feature_count += len(gdf)

        gdf = validate_geometry(
            gdf=gdf,
            dataset_short_name=dataset_short_name,
            page_path=page_path,
        )

        gdf = transform_to_target_crs(
            gdf=gdf,
            target_crs=target_crs,
            dataset_short_name=dataset_short_name,
        )

        gdf = add_audit_columns(
            gdf=gdf,
            manifest=manifest,
            page_path=page_path,
            source_crs=source_crs,
        )

        if gdf.empty:
            LOGGER.warning(
                "%s page=%s has no rows to write.",
                dataset_short_name,
                page_path.name,
            )

            continue

        write_mode = "replace" if first_written_page else "append"

        gdf.to_postgis(
            name=table_name,
            con=engine,
            schema=schema_name,
            if_exists=write_mode,
            index=False,
            chunksize=2000,
            dtype=postgis_dtype_mapping,
        )

        database_feature_count += len(gdf)

        first_written_page = False

        LOGGER.info(
            "%s page=%s wrote=%s cumulative=%s",
            dataset_short_name,
            page_path.name,
            len(gdf),
            database_feature_count,
        )

    if source_feature_count != expected_feature_count:
        raise ValueError(
            f"{dataset_short_name} source feature "
            f"count does not match manifest. "
            f"Manifest={expected_feature_count}, "
            f"source={source_feature_count}"
        )

    if first_written_page:
        raise ValueError(
            f"{dataset_short_name} did not contain " "any valid rows to write."
        )

    return database_feature_count


def create_postgis_indexes(
    *,
    engine: Engine,
    schema_name: str,
    table_name: str,
) -> None:
    """
    Create indexes for the geometry and audit columns.
    """

    # Validate schema and table identifiers before building SQL.
    validate_identifier(
        schema_name,
    )

    validate_identifier(
        table_name,
    )

    # Build deterministic index names.
    geometry_index_name = f"idx_{table_name}_geometry"

    run_id_index_name = f"idx_{table_name}_run_id"

    # Validate generated index identifiers.
    validate_identifier(
        geometry_index_name,
    )

    validate_identifier(
        run_id_index_name,
    )

    # Define index creation and statistics update statements.
    statements = [
        # Spatial index for geometry queries.
        text(
            f"CREATE INDEX IF NOT EXISTS "
            f'"{geometry_index_name}" '
            f'ON "{schema_name}".'
            f'"{table_name}" '
            f'USING GIST ("geometry")'
        ),
        # B-tree index for ingestion run lookups.
        text(
            f"CREATE INDEX IF NOT EXISTS "
            f'"{run_id_index_name}" '
            f'ON "{schema_name}".'
            f'"{table_name}" '
            f'USING BTREE ("ingestion_run_id")'
        ),
        # Refresh planner statistics after indexing.
        text(f'ANALYZE "{schema_name}"."{table_name}"'),
    ]

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(statement)


def validate_database_result(
    *,
    engine: Engine,
    schema_name: str,
    table_name: str,
    expected_count: int,
    target_srid: int,
) -> None:
    """
    Validate database row count, geometry quality, and SRID.
    """

    validate_identifier(
        schema_name,
    )

    validate_identifier(
        table_name,
    )

    # Collect all validation metrics in a single aggregate query.
    query = text(f"""
        SELECT
            COUNT(*) AS row_count,
            COUNT(*) FILTER (
                WHERE geometry IS NULL
            ) AS null_geometry_count,
            COUNT(*) FILTER (
                WHERE ST_IsEmpty(geometry)
            ) AS empty_geometry_count,
            MIN(ST_SRID(geometry)) AS min_srid,
            MAX(ST_SRID(geometry)) AS max_srid
        FROM "{schema_name}"."{table_name}"
        """)

    with engine.connect() as connection:
        # Convert rows to mappings for column-name access,
        # then require and return exactly one summary row.
        row = connection.execute(query).mappings().one()

    actual_count = int(row["row_count"])

    if actual_count != expected_count:
        raise ValueError(
            f"PostGIS row count mismatch for "
            f"{schema_name}.{table_name}. "
            f"Expected={expected_count}, "
            f"actual={actual_count}"
        )

    if int(row["null_geometry_count"]) > 0:
        raise ValueError(f"{schema_name}.{table_name} contains " "NULL geometries.")

    if int(row["empty_geometry_count"]) > 0:
        raise ValueError(f"{schema_name}.{table_name} contains " "empty geometries.")

    min_srid = int(row["min_srid"])

    max_srid = int(row["max_srid"])

    if min_srid != target_srid or max_srid != target_srid:
        raise ValueError(
            f"{schema_name}.{table_name} has "
            f"unexpected SRID range: "
            f"{min_srid} to {max_srid}. "
            f"Expected={target_srid}"
        )


def remove_staging_directory(
    directory: Path,
) -> None:
    """
    Remove the temporary download directory after successful processing.
    """

    if directory.exists():
        shutil.rmtree(directory)


def main() -> int:
    """
    Run the Azure Blob Storage to PostGIS staging load pipeline.
    """

    load_dotenv(PROJECT_ROOT / ".env")

    configure_logging()

    args = parse_arguments()

    # Use user-selected datasets, or load all configured datasets by default.
    selected_datasets = args.datasets or list(DATASET_PROVIDER_MAP.keys())

    # Resolve the target schema from CLI input, environment variable, or default.
    schema_name = args.schema or os.getenv(
        "POSTGIS_TARGET_SCHEMA",
        "staging",
    )

    validate_identifier(
        schema_name,
    )

    # Resolve and create the local staging directory if needed.
    staging_root = args.staging_dir.resolve()

    staging_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    LOGGER.info(
        "Selected datasets: %s",
        selected_datasets,
    )

    LOGGER.info(
        "Target PostGIS schema: %s",
        schema_name,
    )

    # Create one database engine for the full load run.
    engine = create_postgis_engine()

    try:
        test_database_connection(
            engine,
        )

        create_target_schema(
            engine,
            schema_name,
        )

        container_client = get_container_client()

        run_summary: list[dict[str, Any]] = []

        for dataset_short_name in selected_datasets:
            # Look up the source provider for the current dataset.
            provider = DATASET_PROVIDER_MAP[dataset_short_name]

            LOGGER.info(
                "Starting dataset=%s provider=%s",
                dataset_short_name,
                provider,
            )

            (
                manifest,
                dataset_staging_directory,
                page_paths,
            ) = download_dataset_run(
                container_client=container_client,
                provider=provider,
                dataset_short_name=dataset_short_name,
                staging_root=staging_root,
            )

            written_count = write_pages_to_postgis(
                engine=engine,
                schema_name=schema_name,
                table_name=dataset_short_name,
                dataset_short_name=dataset_short_name,
                manifest=manifest,
                page_paths=page_paths,
                target_crs=DEFAULT_TARGET_CRS,
            )

            create_postgis_indexes(
                engine=engine,
                schema_name=schema_name,
                table_name=dataset_short_name,
            )

            validate_database_result(
                engine=engine,
                schema_name=schema_name,
                table_name=dataset_short_name,
                expected_count=written_count,
                target_srid=2193,
            )

            LOGGER.info(
                "Completed dataset=%s " "run_id=%s rows=%s " "table=%s.%s",
                dataset_short_name,
                manifest["run_id"],
                written_count,
                schema_name,
                dataset_short_name,
            )

            run_summary.append(
                {
                    "dataset": dataset_short_name,
                    "provider": provider,
                    "ingestion_run_id": manifest["run_id"],
                    "source_crs": resolve_source_crs(manifest),
                    "target_crs": DEFAULT_TARGET_CRS,
                    "page_count": len(page_paths),
                    "rows_written": written_count,
                    "target_table": f"{schema_name}.{dataset_short_name}",
                }
            )

            if not args.keep_files:
                remove_staging_directory(dataset_staging_directory)

        summary_directory = PROJECT_ROOT / "data" / "staging"

        summary_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        summary_time = datetime.now(
            ZoneInfo(
                "Pacific/Auckland",
            )
        ).strftime(
            "%Y%m%dT%H%M%S%z",
        )

        summary_file = summary_directory / (f"postgis_load_summary_{summary_time}.json")

        # Persist a JSON summary of all datasets processed in this run.
        summary_file.write_text(
            json.dumps(
                run_summary,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        LOGGER.info("All PostGIS loads completed.")

        LOGGER.info(
            "Load summary: %s",
            summary_file,
        )

        return 0

    finally:
        # Always release the engine's pooled database connections.
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
