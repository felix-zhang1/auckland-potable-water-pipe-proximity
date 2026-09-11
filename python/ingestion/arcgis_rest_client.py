from __future__ import annotations

import json
import logging
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger(__name__)

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class ArcGISDownloadResult:
    """
    Store an ArcGIS REST download result.
    """

    dataset: str
    expected_count: int | None
    downloaded_count: int
    page_files: list[Path]
    request_summaries: list[dict[str, Any]]


def redact_url(
    url: str,
) -> str:
    """
    Redact sensitive parameters and summarize Object IDs.
    """

    parts = urlsplit(url)

    query_items = parse_qsl(
        parts.query,
        keep_blank_values=True,
    )

    sensitive_names = {
        "token",
        "key",
        "api_key",
        "apikey",
    }

    redacted_items: list[tuple[str, str]] = []

    for name, value in query_items:
        lower_name = name.lower()

        if lower_name in sensitive_names:
            value = "REDACTED"

        elif lower_name == "objectids":
            id_count = 0 if not value else value.count(",") + 1

            value = f"<{id_count} object IDs>"

        redacted_items.append(
            (
                name,
                value,
            )
        )

    redacted_query = urlencode(
        redacted_items,
        doseq=True,
    )

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            redacted_query,
            parts.fragment,
        )
    )


def chunked(
    values: Sequence[int],
    size: int,
) -> Iterator[list[int]]:
    """
    Split Object IDs into fixed-size batches.
    """

    if size <= 0:
        raise ValueError("Chunk size must be greater than zero.")

    for start in range(
        0,
        len(values),
        size,
    ):
        yield list(values[start : start + size])


class ArcGISRESTClient:
    """
    Access ArcGIS REST layers.
    """

    def __init__(
        self,
        timeout_seconds: int = 180,
    ) -> None:
        """
        Create a session with automatic retries.
        """

        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=1.5,
            status_forcelist=(
                429,
                500,
                502,
                503,
                504,
            ),
            allowed_methods=frozenset(
                {
                    "GET",
                    "POST",
                }
            ),
            respect_retry_after_header=True,
        )

        adapter = HTTPAdapter(
            max_retries=retry,
        )

        self.session = requests.Session()

        self.session.mount(
            "https://",
            adapter,
        )

        self.session.mount(
            "http://",
            adapter,
        )

        self.session.headers.update(
            {
                "User-Agent": "auckland-water-supply-ingestion/1.0",
            }
        )

        self.timeout_seconds = timeout_seconds

    def _read_json_response(
        self,
        response: requests.Response,
        *,
        operation_name: str,
    ) -> dict[str, Any]:
        """
        Validate and parse a JSON response.
        """

        if not response.ok:
            content_type = response.headers.get(
                "Content-Type",
                "",
            )

            raise RuntimeError(
                f"{operation_name} failed.\n"
                f"HTTP status: "
                f"{response.status_code}\n"
                f"Content-Type: "
                f"{content_type}\n"
                f"Request URL: "
                f"{redact_url(response.url)}\n"
                f"Response body:\n"
                f"{response.text[:5000]}"
            )

        try:
            payload = response.json()

        except requests.exceptions.JSONDecodeError as exc:
            content_type = response.headers.get(
                "Content-Type",
                "",
            )

            raise RuntimeError(
                f"{operation_name} returned "
                f"invalid JSON.\n"
                f"HTTP status: "
                f"{response.status_code}\n"
                f"Content-Type: "
                f"{content_type}\n"
                f"Request URL: "
                f"{redact_url(response.url)}\n"
                f"Response body:\n"
                f"{response.text[:5000]}"
            ) from exc

        if not isinstance(
            payload,
            dict,
        ):
            raise RuntimeError(f"{operation_name} did not return " "a JSON object.")

        if "error" in payload:
            raise RuntimeError(
                f"{operation_name} returned "
                f"an ArcGIS error.\n"
                f"Request URL: "
                f"{redact_url(response.url)}\n"
                f"Error:\n"
                f"{json.dumps(payload['error'], indent=2)}"
            )

        return payload

    def get_layer_metadata(
        self,
        layer_url: str,
    ) -> dict[str, Any]:
        """
        Retrieve the layer definition when available.
        """

        response = self.session.get(
            layer_url,
            params={
                "f": "json",
            },
            timeout=self.timeout_seconds,
        )

        return self._read_json_response(
            response,
            operation_name=("ArcGIS layer metadata request"),
        )

    def _build_spatial_filter_params(
        self,
        *,
        dataset: dict[str, Any],
        bbox: BBox | None,
    ) -> dict[str, Any]:
        """
        Build ArcGIS REST BBOX query parameters.
        """

        filter_config = dataset.get(
            "filter",
            {
                "type": "none",
            },
        )

        filter_type = filter_config.get(
            "type",
            "none",
        )

        if filter_type == "none":
            return {}

        if filter_type != "bbox":
            raise ValueError(
                f"{dataset['short_name']} uses "
                f"unsupported ArcGIS filter type: "
                f"{filter_type}"
            )

        if bbox is None:
            raise ValueError(f"{dataset['short_name']} requires " "a BBOX.")

        minx, miny, maxx, maxy = bbox

        if minx >= maxx:
            raise ValueError(
                f"Invalid BBOX: minx must be " f"smaller than maxx. BBOX={bbox}"
            )

        if miny >= maxy:
            raise ValueError(
                f"Invalid BBOX: miny must be " f"smaller than maxy. BBOX={bbox}"
            )

        buffer_distance = float(
            filter_config.get(
                "buffer",
                0,
            )
        )

        if buffer_distance < 0:
            raise ValueError("BBOX buffer cannot be negative.")

        minx -= buffer_distance
        miny -= buffer_distance
        maxx += buffer_distance
        maxy += buffer_distance

        in_sr = int(
            dataset.get(
                "in_sr",
                2193,
            )
        )

        return {
            "geometry": (f"{minx}," f"{miny}," f"{maxx}," f"{maxy}"),
            "geometryType": ("esriGeometryEnvelope"),
            "spatialRel": ("esriSpatialRelIntersects"),
            "inSR": in_sr,
        }

    def get_object_ids(
        self,
        *,
        query_url: str,
        dataset: dict[str, Any],
        bbox: BBox | None,
    ) -> tuple[
        str,
        list[int],
        str,
    ]:
        """
        Retrieve matching Object IDs using a BBOX.
        """

        params: dict[str, Any] = {
            "where": str(
                dataset.get(
                    "where",
                    "1=1",
                )
            ),
            "returnIdsOnly": "true",
            "returnGeometry": "false",
            "f": "json",
        }

        params.update(
            self._build_spatial_filter_params(
                dataset=dataset,
                bbox=bbox,
            )
        )

        response = self.session.get(
            query_url,
            params=params,
            timeout=self.timeout_seconds,
        )

        payload = self._read_json_response(
            response,
            operation_name=(f"{dataset['short_name']} " "Object ID request"),
        )

        object_id_field = payload.get("objectIdFieldName") or payload.get(
            "objectIdField"
        )

        object_ids = payload.get(
            "objectIds",
        )

        if not object_id_field:
            raise RuntimeError(
                f"{dataset['short_name']} Object ID "
                "response does not contain "
                "'objectIdFieldName'.\n"
                f"Response:\n"
                f"{json.dumps(payload, indent=2)[:5000]}"
            )

        if not isinstance(
            object_ids,
            list,
        ):
            raise RuntimeError(
                f"{dataset['short_name']} Object ID "
                "response does not contain "
                "an 'objectIds' list.\n"
                f"Response:\n"
                f"{json.dumps(payload, indent=2)[:5000]}"
            )

        normalized_ids = sorted({int(value) for value in object_ids})

        return (
            str(object_id_field),
            normalized_ids,
            redact_url(response.url),
        )

    def download_pages(
        self,
        *,
        query_url: str,
        dataset: dict[str, Any],
        output_dir: Path,
        bbox: BBox | None = None,
        layer_metadata: dict[str, Any] | None = None,
    ) -> ArcGISDownloadResult:
        """
        Download GeoJSON in Object ID batches.
        """

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        out_fields = str(
            dataset.get(
                "out_fields",
                "*",
            )
        )

        out_sr = int(
            dataset.get(
                "out_sr",
                2193,
            )
        )

        configured_page_size = int(
            dataset.get(
                "page_size",
                500,
            )
        )

        metadata_limit: int | None = None

        if layer_metadata:
            raw_limit = layer_metadata.get(
                "maxRecordCount",
            )

            if raw_limit is not None:
                metadata_limit = int(raw_limit)

        page_size = min(
            configured_page_size,
            metadata_limit or configured_page_size,
        )

        if page_size <= 0:
            raise ValueError(
                f"{dataset['short_name']} has " f"an invalid page_size: " f"{page_size}"
            )

        (
            object_id_field,
            object_ids,
            ids_request_url,
        ) = self.get_object_ids(
            query_url=query_url,
            dataset=dataset,
            bbox=bbox,
        )

        expected_count = len(
            object_ids,
        )

        LOGGER.info(
            "%s BBOX query returned %s IDs; " "page_size=%s; outSR=EPSG:%s",
            dataset["short_name"],
            expected_count,
            page_size,
            out_sr,
        )

        request_summaries: list[dict[str, Any]] = [
            {
                "request_type": "object_ids",
                "http_method": "GET",
                "pagination_method": "object_ids",
                "returned_id_count": expected_count,
                "object_id_field": object_id_field,
                "request_url_redacted": ids_request_url,
            }
        ]

        page_files: list[Path] = []

        downloaded_count = 0

        if expected_count == 0:
            return ArcGISDownloadResult(
                dataset=dataset["short_name"],
                expected_count=0,
                downloaded_count=0,
                page_files=[],
                request_summaries=(request_summaries),
            )

        batches = chunked(
            object_ids,
            page_size,
        )

        for page_number, page_ids in enumerate(
            batches,
            start=1,
        ):
            output_format = str(
                dataset.get(
                    "output_format",
                    "json",
                )
            )

            params: dict[str, Any] = {
                "objectIds": ",".join(
                    map(
                        str,
                        page_ids,
                    )
                ),
                "outFields": out_fields,
                "returnGeometry": "true",
                "outSR": out_sr,
                "f": output_format,
            }

            started_at = time.monotonic()

            response = self.session.post(
                query_url,
                data=params,
                timeout=self.timeout_seconds,
            )

            elapsed_seconds = round(
                time.monotonic() - started_at,
                3,
            )

            payload = self._read_json_response(
                response,
                operation_name=(
                    f"{dataset['short_name']} " f"page {page_number} request"
                ),
            )

            features = payload.get(
                "features",
            )

            if not isinstance(
                features,
                list,
            ):
                raise RuntimeError(
                    f"{dataset['short_name']} page "
                    f"{page_number} does not contain "
                    "an ArcGIS features list."
                )

            requested_ids = set(
                page_ids,
            )

            returned_ids: set[int] = set()

            for feature in features:
                if not isinstance(
                    feature,
                    dict,
                ):
                    continue

                attributes = feature.get(
                    "attributes",
                )

                if not isinstance(
                    attributes,
                    dict,
                ):
                    continue

                returned_object_id = attributes.get(
                    object_id_field,
                )

                if returned_object_id is not None:
                    returned_ids.add(int(returned_object_id))

            missing_ids = sorted(requested_ids - returned_ids)

            unexpected_ids = sorted(returned_ids - requested_ids)

            if missing_ids or unexpected_ids or len(features) != len(page_ids):
                raise RuntimeError(
                    f"{dataset['short_name']} page "
                    f"{page_number} failed "
                    "Object ID validation.\n"
                    f"Requested count: "
                    f"{len(page_ids)}\n"
                    f"Returned count: "
                    f"{len(features)}\n"
                    f"Missing IDs sample: "
                    f"{missing_ids[:20]}\n"
                    f"Unexpected IDs sample: "
                    f"{unexpected_ids[:20]}"
                )

            page_file = output_dir / f"page_{page_number:05d}.json"

            temporary_file = output_dir / (f"page_{page_number:05d}" ".json.tmp")

            temporary_file.write_bytes(
                response.content,
            )

            temporary_file.replace(
                page_file,
            )

            page_files.append(
                page_file,
            )

            feature_count = len(
                features,
            )

            downloaded_count += feature_count

            request_summaries.append(
                {
                    "request_type": "feature_page",
                    "http_method": "POST",
                    "page_number": page_number,
                    "pagination_method": "object_ids",
                    "requested_count": len(
                        page_ids,
                    ),
                    "returned_count": feature_count,
                    "first_object_id": page_ids[0],
                    "last_object_id": page_ids[-1],
                    "return_geometry": True,
                    "out_fields": out_fields,
                    "out_sr": out_sr,
                    "output_format": output_format,
                    "http_status": response.status_code,
                    "content_type": response.headers.get(
                        "Content-Type",
                    ),
                    "content_length_bytes": len(
                        response.content,
                    ),
                    "elapsed_seconds": elapsed_seconds,
                    "request_url_redacted": redact_url(
                        response.url,
                    ),
                }
            )

            LOGGER.info(
                "%s page=%s returned=%s " "cumulative=%s/%s",
                dataset["short_name"],
                page_number,
                feature_count,
                downloaded_count,
                expected_count,
            )

        if downloaded_count != expected_count:
            raise RuntimeError(
                f"{dataset['short_name']} downloaded "
                "feature count does not match the "
                "Object ID count.\n"
                f"Expected: {expected_count}\n"
                f"Downloaded: {downloaded_count}"
            )

        return ArcGISDownloadResult(
            dataset=dataset["short_name"],
            expected_count=expected_count,
            downloaded_count=(downloaded_count),
            page_files=page_files,
            request_summaries=(request_summaries),
        )
