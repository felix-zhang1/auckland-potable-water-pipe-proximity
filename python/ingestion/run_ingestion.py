from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from dotenv import load_dotenv

from arcgis_rest_client import ArcGISRESTClient
from blob_uploader import upload_directory
from metadata import (
    build_page_metadata,
    write_manifest,
)
from bbox import calculate_total_bounds
from wfs_client import WFSClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "datasets.yaml"

DEFAULT_RAW_DIRECTORY = PROJECT_ROOT / "data" / "raw"


def load_config(
    config_path: Path,
) -> list[dict[str, Any]]:
    """
    Load and validate datasets.yaml.
    """

    config_text = config_path.read_text(
        encoding="utf-8",
    )

    config_data = yaml.safe_load(
        config_text,
    )

    if not isinstance(
        config_data,
        dict,
    ):
        raise ValueError("datasets.yaml must contain " "a top-level YAML object.")

    datasets = config_data.get(
        "datasets",
    )

    if not isinstance(
        datasets,
        list,
    ):
        raise ValueError("datasets.yaml must contain " "a 'datasets' list.")

    if not datasets:
        raise ValueError("The datasets list cannot be empty.")

    required_common_fields = {
        "name",
        "short_name",
        "provider",
    }

    supported_source_types = {
        "wfs",
        "arcgis_rest",
    }

    seen_short_names: set[str] = set()

    for dataset in datasets:
        if not isinstance(
            dataset,
            dict,
        ):
            raise ValueError("Every dataset configuration must be a YAML object.")

        missing_common_fields = required_common_fields - dataset.keys()

        if missing_common_fields:
            raise ValueError(
                "Dataset is missing required fields: "
                f"{sorted(missing_common_fields)}"
            )

        short_name = str(
            dataset["short_name"],
        )

        if short_name in seen_short_names:
            raise ValueError("Duplicate dataset short_name: " f"{short_name}")

        seen_short_names.add(
            short_name,
        )

        source_type = dataset.get(
            "source_type",
            "wfs",
        )

        if source_type not in supported_source_types:
            raise ValueError(f"{short_name} has unsupported source_type: {source_type}")

        if source_type == "wfs":
            required_wfs_fields = {
                "base_url_template",
                "api_key_env",
                "type_name",
                "wfs_version",
                "output_format",
                "srs_name",
                "filter",
                "sort_by",
            }

            missing_wfs_fields = required_wfs_fields - dataset.keys()

            if missing_wfs_fields:
                raise ValueError(
                    f"{short_name} is missing WFS "
                    f"fields: "
                    f"{sorted(missing_wfs_fields)}"
                )

            sort_by = str(
                dataset["sort_by"],
            ).strip()

            if not sort_by:
                raise ValueError(f"{short_name} has an empty WFS sort_by value.")

        elif source_type == "arcgis_rest":
            required_arcgis_fields = {
                "layer_url",
                "query_url",
                "output_format",
            }

            missing_arcgis_fields = required_arcgis_fields - dataset.keys()

            if missing_arcgis_fields:
                raise ValueError(
                    f"{short_name} is missing "
                    f"ArcGIS REST fields: "
                    f"{sorted(missing_arcgis_fields)}"
                )

    return datasets


def resolve_wfs_base_url(
    dataset: dict[str, Any],
) -> str:
    """
    Build the WFS URL using the API key.
    """

    api_key_environment_name = dataset["api_key_env"]

    api_key = os.getenv(
        api_key_environment_name,
    )

    if not api_key:
        raise EnvironmentError(f"{api_key_environment_name} " "is not set.")

    return dataset["base_url_template"].format(
        api_key=api_key,
    )


def configure_logging(
    log_file: Path,
) -> None:
    """
    Log to both the console and a file.
    """

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
    Parse command-line arguments.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Download configured spatial datasets "
            "locally and upload them to "
            "Azure Blob Storage."
        )
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to datasets.yaml.",
    )

    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIRECTORY,
        help="Local raw data directory.",
    )

    parser.add_argument(
        "--datasets",
        nargs="*",
        help=("Optional dataset short names. " "Default: run all configured datasets."),
    )

    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help=("Download locally without uploading " "to Azure Blob Storage."),
    )

    return parser.parse_args()


def select_datasets(
    datasets: list[dict[str, Any]],
    selected_dataset_names: set[str],
) -> list[dict[str, Any]]:
    """
    Select datasets and include BBOX dependencies.
    """

    if not selected_dataset_names:
        return datasets

    dataset_by_short_name = {dataset["short_name"]: dataset for dataset in datasets}

    missing_dataset_names = selected_dataset_names - dataset_by_short_name.keys()

    if missing_dataset_names:
        raise ValueError(
            "Unknown dataset short names: " f"{sorted(missing_dataset_names)}"
        )

    required_dataset_names = set(
        selected_dataset_names,
    )

    dependencies_added = True

    while dependencies_added:
        dependencies_added = False

        for short_name in list(
            required_dataset_names,
        ):
            dataset = dataset_by_short_name[short_name]

            filter_config = dataset.get(
                "filter",
                {
                    "type": "none",
                },
            )

            if (
                filter_config.get(
                    "type",
                )
                != "bbox"
            ):
                continue

            bbox_source = filter_config.get(
                "bbox_source",
            )

            if not bbox_source:
                raise ValueError(
                    f"{short_name} uses a BBOX "
                    "filter but does not define "
                    "bbox_source."
                )

            if bbox_source not in dataset_by_short_name:
                raise ValueError(
                    f"{short_name} depends on " f"unknown dataset: {bbox_source}"
                )

            if bbox_source not in required_dataset_names:
                required_dataset_names.add(
                    bbox_source,
                )

                dependencies_added = True

    # Preserve config order so BBOX sources run first.
    return [
        dataset
        for dataset in datasets
        if dataset["short_name"] in required_dataset_names
    ]


def create_dataset_directories(
    *,
    raw_directory: Path,
    provider: str,
    short_name: str,
    ingestion_date: str,
    run_id: str,
) -> tuple[Path, Path, Path]:
    """
    Create local dataset directories.
    """

    provider_directory_name = provider.lower().replace(
        " ",
        "_",
    )

    dataset_directory = (
        raw_directory
        / provider_directory_name
        / short_name
        / f"ingestion_date={ingestion_date}"
        / run_id
    )

    metadata_directory = dataset_directory / "metadata"

    pages_directory = dataset_directory / "pages"

    metadata_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    pages_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return (
        dataset_directory,
        metadata_directory,
        pages_directory,
    )


def find_latest_previous_manifest(
    dataset_root: Path,
    *,
    current_run_id: str,
) -> tuple[Path | None, dict[str, Any] | None]:
    """
    Find the latest previous manifest containing a dataset SHA-256.

    Manifests created before change detection was implemented are
    ignored because they do not contain dataset_sha256.
    """

    candidates: list[
        tuple[
            datetime,
            Path,
            dict[str, Any],
        ]
    ] = []

    for manifest_path in dataset_root.rglob(
        "manifest.json",
    ):
        try:
            manifest = json.loads(
                manifest_path.read_text(
                    encoding="utf-8",
                )
            )

        except (
            OSError,
            json.JSONDecodeError,
        ):
            continue

        if manifest.get("run_id") == current_run_id:
            continue

        if not manifest.get("dataset_sha256"):
            continue

        downloaded_at_value = manifest.get(
            "downloaded_at",
        )

        if not downloaded_at_value:
            continue

        try:
            downloaded_at = datetime.fromisoformat(
                str(downloaded_at_value),
            )

        except ValueError:
            continue

        candidates.append(
            (
                downloaded_at,
                manifest_path,
                manifest,
            )
        )

    if not candidates:
        return (
            None,
            None,
        )

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    _, manifest_path, manifest = candidates[0]

    return (
        manifest_path,
        manifest,
    )


def main() -> int:
    """
    Run the spatial data ingestion pipeline.
    """

    args = parse_arguments()

    load_dotenv(PROJECT_ROOT / ".env")

    now = datetime.now(
        ZoneInfo(
            "Pacific/Auckland",
        )
    )

    ingestion_date = now.date().isoformat()

    run_id = now.strftime(
        "%Y%m%dT%H%M%S%z",
    )

    log_file = PROJECT_ROOT / "logs" / f"ingestion_{run_id}.log"

    configure_logging(
        log_file,
    )

    logger = logging.getLogger(
        "run_ingestion",
    )

    datasets = load_config(
        args.config,
    )

    selected_dataset_names = set(args.datasets or [])

    datasets = select_datasets(
        datasets,
        selected_dataset_names,
    )

    wfs_client = WFSClient()

    arcgis_rest_client = ArcGISRESTClient()

    completed_datasets: dict[
        str,
        dict[str, Any],
    ] = {}

    run_summary: list[dict[str, Any]] = []

    for dataset in datasets:
        short_name = dataset["short_name"]

        source_type = dataset.get(
            "source_type",
            "wfs",
        )

        logger.info(
            "Starting dataset: %s " "(source_type=%s)",
            short_name,
            source_type,
        )

        filter_config = dataset.get(
            "filter",
            {
                "type": "none",
            },
        )

        bbox: (
            tuple[
                float,
                float,
                float,
                float,
            ]
            | None
        ) = None

        if (
            filter_config.get(
                "type",
            )
            == "bbox"
        ):
            bbox_source_name = filter_config["bbox_source"]

            bbox_source = completed_datasets.get(
                bbox_source_name,
            )

            if bbox_source is None:
                raise RuntimeError(
                    f"{short_name} depends on "
                    f"{bbox_source_name}. "
                    "The BBOX source must be "
                    "completed earlier in the "
                    "same ingestion execution."
                )

            target_crs = dataset.get(
                "srs_name",
            )

            if not target_crs:
                raise ValueError(
                    f"{short_name} uses a BBOX filter " "but does not define srs_name."
                )

            bbox = calculate_total_bounds(
                page_files=bbox_source["page_files"],
                expected_crs=target_crs,
            )

            logger.info(
                "%s calculated BBOX=%s",
                short_name,
                bbox,
            )

        (
            dataset_directory,
            metadata_directory,
            pages_directory,
        ) = create_dataset_directories(
            raw_directory=args.raw_dir,
            provider=dataset["provider"],
            short_name=short_name,
            ingestion_date=ingestion_date,
            run_id=run_id,
        )

        if source_type == "wfs":
            base_url = resolve_wfs_base_url(
                dataset,
            )

            logger.info(
                "Checking GetCapabilities " "for %s",
                short_name,
            )

            capabilities_response = wfs_client.get_capabilities(
                base_url=base_url,
                version=dataset["wfs_version"],
            )

            capabilities_file = metadata_directory / "get_capabilities.xml"

            capabilities_file.write_bytes(
                capabilities_response.content,
            )

            logger.info(
                "Downloading WFS dataset: %s sort_by=%s",
                short_name,
                dataset["sort_by"]
            )

            download_result = wfs_client.download_pages(
                base_url=base_url,
                dataset=dataset,
                output_dir=pages_directory,
                bbox=bbox,
            )

        elif source_type == "arcgis_rest":
            layer_url = str(dataset["layer_url"])

            query_url = str(dataset["query_url"])

            layer_metadata: dict[str, Any] | None = None

            fetch_layer_metadata = bool(
                dataset.get(
                    "fetch_layer_metadata",
                    True,
                )
            )

            if fetch_layer_metadata:
                logger.info(
                    "Reading ArcGIS layer " "metadata for %s",
                    short_name,
                )

                layer_metadata = arcgis_rest_client.get_layer_metadata(
                    layer_url=layer_url,
                )

                layer_metadata_file = metadata_directory / "layer_definition.json"

                layer_metadata_file.write_text(
                    json.dumps(
                        layer_metadata,
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            else:
                logger.info(
                    "Skipping ArcGIS layer "
                    "metadata request for %s "
                    "because "
                    "fetch_layer_metadata=false",
                    short_name,
                )

            logger.info(
                "Downloading ArcGIS REST " "dataset: %s",
                short_name,
            )

            download_result = arcgis_rest_client.download_pages(
                query_url=query_url,
                dataset=dataset,
                layer_metadata=(layer_metadata),
                output_dir=(pages_directory),
                bbox=bbox,
            )

        else:
            raise ValueError(
                f"Unsupported source_type " f"'{source_type}' for " f"{short_name}."
            )

        logger.info(
            "%s expected=%s " "downloaded=%s pages=%s",
            short_name,
            download_result.expected_count,
            download_result.downloaded_count,
            len(
                download_result.page_files,
            ),
        )

        pages_metadata, dataset_sha256 = build_page_metadata(
            download_result.page_files,
        )

        # dataset_directory:
        # raw/provider/dataset/ingestion_date/run_id
        #
        # parents[1]:
        # raw/provider/dataset
        dataset_root = dataset_directory.parents[1]

        (
            previous_manifest_path,
            previous_manifest,
        ) = find_latest_previous_manifest(
            dataset_root,
            current_run_id=run_id,
        )

        previous_run_id: str | None = None

        previous_dataset_sha256: str | None = None

        if previous_manifest is not None:
            previous_run_id_value = previous_manifest.get(
                "run_id",
            )

            if previous_run_id_value is not None:
                previous_run_id = str(
                    previous_run_id_value,
                )

            previous_sha256_value = previous_manifest.get(
                "dataset_sha256",
            )

            if previous_sha256_value is not None:
                previous_dataset_sha256 = str(
                    previous_sha256_value,
                )

        changed = previous_dataset_sha256 != dataset_sha256

        if previous_manifest is None:
            logger.info(
                "%s has no previous comparable manifest; "
                "treating the dataset as changed.",
                short_name,
            )

        elif changed:
            logger.info(
                "%s changed since run %s.",
                short_name,
                previous_run_id,
            )

        else:
            logger.info(
                "%s is unchanged since run %s.",
                short_name,
                previous_run_id,
            )

        logger.info(
            "%s dataset_sha256=%s",
            short_name,
            dataset_sha256,
        )

        if previous_manifest_path is not None:
            logger.info(
                "%s previous_manifest=%s",
                short_name,
                previous_manifest_path,
            )

        manifest_path = write_manifest(
            dataset_directory / "manifest.json",
            dataset=dataset,
            run_id=run_id,
            downloaded_at=now.isoformat(),
            expected_count=download_result.expected_count,
            downloaded_count=download_result.downloaded_count,
            pages=pages_metadata,
            dataset_sha256=dataset_sha256,
            changed=changed,
            previous_run_id=previous_run_id,
            request_summaries=download_result.request_summaries,
            bbox=bbox,
        )

        uploaded_blob_names: list[str] = []

        upload_status: str

        if args.skip_upload:
            upload_status = "disabled"

            logger.info(
                "Upload disabled for %s because " "--skip-upload was supplied.",
                short_name,
            )

        elif not changed:
            upload_status = "skipped_unchanged"

            logger.info(
                "Skipping Azure upload for %s because "
                "the downloaded dataset is unchanged.",
                short_name,
            )

        else:
            upload_status = "uploaded"

            logger.info(
                "Uploading %s to Azure Blob Storage",
                short_name,
            )

            uploaded_blob_names = upload_directory(
                local_dir=dataset_directory,
                provider=dataset["provider"],
                dataset_short_name=short_name,
                ingestion_date=ingestion_date,
                run_id=run_id,
            )

            logger.info(
                "Uploaded %s files for %s",
                len(
                    uploaded_blob_names,
                ),
                short_name,
            )

        completed_datasets[short_name] = {
            "page_files": (download_result.page_files),
            "manifest": manifest_path,
        }

        run_summary.append(
            {
                "dataset": short_name,
                "source_type": source_type,
                "expected_count": download_result.expected_count,
                "downloaded_count": download_result.downloaded_count,
                "page_count": len(download_result.page_files),
                "dataset_sha256": dataset_sha256,
                "changed": changed,
                "previous_run_id": previous_run_id,
                "local_directory": str(dataset_directory),
                "upload_status": upload_status,
                "uploaded_blob_count": len(uploaded_blob_names),
            }
        )

    run_summary_file = args.raw_dir / f"run_summary_{run_id}.json"

    run_summary_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    run_summary_file.write_text(
        json.dumps(
            run_summary,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    logger.info("Ingestion completed.")

    logger.info(
        "Run summary: %s",
        run_summary_file,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
