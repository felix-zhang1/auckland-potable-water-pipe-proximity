from __future__ import annotations

import os

from pathlib import Path

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import (
    BlobServiceClient,
    ContentSettings,
)


def upload_directory(
    local_dir: Path,
    *,
    provider: str,
    dataset_short_name: str,
    ingestion_date: str,
    run_id: str,
) -> list[str]:
    """
    Upload all files in a local directory to Azure Blob Storage.

    Blob path:
    provider/dataset/ingestion_date=YYYY-MM-DD/run_id/filename
    """

    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

    container_name = os.getenv(
        "AZURE_STORAGE_RAW_CONTAINER_NAME=raw",
        "raw",
    )

    if not connection_string:
        raise EnvironmentError("AZURE_STORAGE_CONNECTION_STRING is not set.")

    blob_service_client = BlobServiceClient.from_connection_string(connection_string)

    container_client = blob_service_client.get_container_client(container_name)

    try:
        container_client.create_container()

    except ResourceExistsError:
        pass

    provider_slug = provider.lower().replace(
        " ",
        "_",
    )

    blob_prefix = (
        f"{provider_slug}/"
        f"{dataset_short_name}/"
        f"ingestion_date={ingestion_date}/"
        f"{run_id}"
    )

    uploaded_blob_names: list[str] = []

    local_files = sorted(path for path in local_dir.rglob("*") if path.is_file())

    for file_path in local_files:
        relative_path = file_path.relative_to(local_dir).as_posix()

        blob_name = f"{blob_prefix}/" f"{relative_path}"

        suffix = file_path.suffix.lower()

        if suffix == ".geojson":
            content_type = "application/geo+json"

        elif suffix == ".json":
            content_type = "application/json"

        elif suffix == ".xml":
            content_type = "application/xml"

        else:
            content_type = "application/octet-stream"

        with file_path.open("rb") as source:
            container_client.upload_blob(
                name=blob_name,
                data=source,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )

        uploaded_blob_names.append(blob_name)

    return uploaded_blob_names
