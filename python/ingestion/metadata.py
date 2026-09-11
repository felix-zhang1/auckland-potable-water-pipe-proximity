from __future__ import annotations

import hashlib
import json

from pathlib import Path
from typing import Any


def sha256_file(
    path: Path,
    chunk_size: int = 1024 * 1024,
) -> str:
    """
    Calculate a file's raw SHA-256 hash.

    This hash is calculated directly from the downloaded file bytes.
    It is used for file-integrity verification.
    """

    digest = hashlib.sha256()

    with path.open("rb") as source:
        for chunk in iter(
            lambda: source.read(chunk_size),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def canonical_page_sha256(
    path: Path,
) -> str:
    """
    Calculate a stable SHA-256 hash for JSON or GeoJSON content.

    Volatile response metadata is excluded from this fingerprint.
    The original downloaded file is not modified.

    The following top-level fields are excluded:

    - timeStamp: normally changes on every WFS request;
    - links: may contain request-specific pagination URLs.

    JSON object keys are also sorted before hashing, so differences
    in key order or whitespace do not count as dataset changes.
    """

    payload = json.loads(
        path.read_text(
            encoding="utf-8",
        )
    )

    if not isinstance(
        payload,
        dict,
    ):
        raise ValueError(f"Expected a top-level JSON object in {path}.")

    # Create a shallow copy so the parsed payload is not modified.
    canonical_payload = dict(
        payload,
    )

    canonical_payload.pop(
        "timeStamp",
        None,
    )

    canonical_payload.pop(
        "links",
        None,
    )

    canonical_bytes = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(
            ",",
            ":",
        ),
        ensure_ascii=False,
    ).encode(
        "utf-8",
    )

    return hashlib.sha256(
        canonical_bytes,
    ).hexdigest()


def build_page_metadata(
    page_files: list[Path],
) -> tuple[list[dict[str, Any]], str]:
    """
    Build page metadata and calculate one stable dataset fingerprint.

    Two page hashes are calculated:

    sha256:
        Hash of the original downloaded file bytes.

    content_sha256:
        Hash of normalized JSON content after volatile response
        metadata has been removed.

    The combined dataset_sha256 is built from each filename and its
    content_sha256. It is used for upload change detection.
    """

    pages: list[dict[str, Any]] = []

    dataset_digest = hashlib.sha256()

    for page_file in sorted(
        page_files,
        key=lambda path: path.name,
    ):
        file_sha256 = sha256_file(
            page_file,
        )

        content_sha256 = canonical_page_sha256(
            page_file,
        )

        pages.append(
            {
                "filename": page_file.name,
                "size_bytes": page_file.stat().st_size,
                "sha256": file_sha256,
                "content_sha256": content_sha256,
            }
        )

        dataset_digest.update(
            page_file.name.encode(
                "utf-8",
            )
        )

        dataset_digest.update(
            content_sha256.encode(
                "ascii",
            )
        )

    return (
        pages,
        dataset_digest.hexdigest(),
    )


def write_manifest(
    path: Path,
    *,
    dataset: dict[str, Any],
    run_id: str,
    downloaded_at: str,
    expected_count: int | None,
    downloaded_count: int,
    pages: list[dict[str, Any]],
    dataset_sha256: str,
    changed: bool,
    previous_run_id: str | None,
    request_summaries: list[dict[str, Any]],
    bbox: (
        tuple[
            float,
            float,
            float,
            float,
        ]
        | None
    ),
) -> Path:
    """
    Write an ingestion manifest for WFS or ArcGIS REST data.
    """

    source_type = dataset.get(
        "source_type",
        "wfs",
    )

    source_configuration: dict[str, Any]

    if source_type == "wfs":
        source_configuration = {
            "base_url_template": dataset.get(
                "base_url_template",
            ),
            "type_name": dataset.get(
                "type_name",
            ),
            "wfs_version": dataset.get(
                "wfs_version",
            ),
            "output_format": dataset.get(
                "output_format",
            ),
            "srs_name": dataset.get(
                "srs_name",
            ),
            "page_size": dataset.get(
                "page_size",
            ),
            "sort_by": dataset.get(
                "sort_by",
            ),
            "filter": dataset.get(
                "filter",
            ),
        }

    elif source_type == "arcgis_rest":
        source_configuration = {
            "layer_url": dataset.get(
                "layer_url",
            ),
            "query_url": dataset.get(
                "query_url",
            ),
            "where": dataset.get(
                "where",
                "1=1",
            ),
            "out_fields": dataset.get(
                "out_fields",
                "*",
            ),
            "srs_name": dataset.get(
                "srs_name",
            ),
            "in_sr": dataset.get(
                "in_sr",
            ),
            "out_sr": dataset.get(
                "out_sr",
            ),
            "output_format": dataset.get(
                "output_format",
            ),
            "page_size": dataset.get(
                "page_size",
            ),
            "pagination_method": "object_ids",
            "filter": dataset.get(
                "filter",
            ),
        }

    else:
        source_configuration = {
            "filter": dataset.get(
                "filter",
            ),
        }

    manifest = {
        "run_id": run_id,
        "downloaded_at": downloaded_at,
        "dataset_name": dataset["name"],
        "short_name": dataset["short_name"],
        "provider": dataset["provider"],
        "source_type": source_type,
        "source_configuration": source_configuration,
        "bbox_used": bbox,
        "bbox_crs": (
            dataset.get(
                "srs_name",
            )
            if bbox is not None
            else None
        ),
        "expected_count": expected_count,
        "downloaded_count": downloaded_count,
        "page_count": len(
            pages,
        ),
        "dataset_sha256": dataset_sha256,
        "changed": changed,
        "previous_run_id": previous_run_id,
        "pages": pages,
        "requests": request_summaries,
    }

    path.write_text(
        json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return path
