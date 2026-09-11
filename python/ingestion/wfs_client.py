from __future__ import annotations

import json
import logging
import time
import xml.etree.ElementTree as ET

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadResult:
    """
    Stores the download result for one dataset.
    """

    dataset: str
    expected_count: int | None
    downloaded_count: int
    page_files: list[Path]
    request_summaries: list[dict[str, Any]]


def build_fes_equal_filter(
    field: str,
    value: str,
) -> str:
    """
    Builds an FES 2.0 equality filter for use in a WFS 2.0 request.

    Example output:

    <fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">
        <fes:PropertyIsEqualTo>
            <fes:ValueReference>town_city</fes:ValueReference>
            <fes:Literal>Auckland</fes:Literal>
        </fes:PropertyIsEqualTo>
    </fes:Filter>
    """

    fes_namespace = "http://www.opengis.net/fes/2.0"

    # Use the `fes` prefix instead of an auto-generated one.
    ET.register_namespace(
        "fes",
        fes_namespace,
    )

    root = ET.Element(f"{{{fes_namespace}}}Filter")

    equal_element = ET.SubElement(
        root,
        f"{{{fes_namespace}}}PropertyIsEqualTo",
    )

    value_reference = ET.SubElement(
        equal_element,
        f"{{{fes_namespace}}}ValueReference",
    )
    value_reference.text = field

    literal = ET.SubElement(
        equal_element,
        f"{{{fes_namespace}}}Literal",
    )
    literal.text = value

    return ET.tostring(
        root,
        encoding="unicode",
    )


def redact_url(url: str) -> str:
    """
    Redacts API keys from URLs before logging.
    """

    parts = urlsplit(url)
    path = parts.path

    marker = "services;key="

    if marker in path:
        before, remainder = path.split(
            marker,
            1,
        )

        if "/" in remainder:
            _, after = remainder.split(
                "/",
                1,
            )

            path = f"{before}{marker}REDACTED/{after}"

        else:
            path = f"{before}{marker}REDACTED"

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            path,
            parts.query,
            parts.fragment,
        )
    )


class WFSClient:
    """
    Handles WFS HTTP requests.
    """

    def __init__(
        self,
        timeout_seconds: int = 180,
    ) -> None:
        """
        Creates a requests session with retries.
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
                }
            ),
            respect_retry_after_header=True,
        )

        adapter = HTTPAdapter(max_retries=retry)

        self.session = requests.Session()

        self.session.mount(
            "https://",
            adapter,
        )

        self.session.mount(
            "http://",
            adapter,
        )

        self.timeout_seconds = timeout_seconds

    def get_capabilities(
        self,
        base_url: str,
        version: str,
    ) -> requests.Response:
        """
        Downloads the GetCapabilities XML.
        """

        params = {
            "service": "WFS",
            "version": version,
            "request": "GetCapabilities",
        }

        response = self.session.get(
            base_url,
            params=params,
            timeout=self.timeout_seconds,
        )

        if not response.ok:
            content_type = response.headers.get(
                "Content-Type",
                "",
            )

            raise RuntimeError(
                "GetCapabilities request failed.\n"
                f"HTTP status: {response.status_code}\n"
                f"Content-Type: {content_type}\n"
                f"Request URL: "
                f"{redact_url(response.url)}\n"
                f"Response body:\n"
                f"{response.text[:5000]}"
            )

        return response

    def _base_getfeature_params(
        self,
        dataset: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Builds shared GetFeature parameters.
        """

        return {
            "service": "WFS",
            "version": dataset["wfs_version"],
            "request": "GetFeature",
            "typeNames": dataset["type_name"],
            "outputFormat": dataset["output_format"],
            "srsName": dataset["srs_name"],
        }

    def _add_filter(
        self,
        params: dict[str, Any],
        dataset: dict[str, Any],
        bbox: (
            tuple[
                float,
                float,
                float,
                float,
            ]
            | None
        ),
    ) -> None:
        """
        Adds an attribute or BBOX filter.
        """

        filter_config = dataset["filter"]
        filter_type = filter_config["type"]

        if filter_type == "attribute":
            params["FILTER"] = build_fes_equal_filter(
                field=filter_config["field"],
                value=str(filter_config["value"]),
            )

        elif filter_type == "bbox":
            if bbox is None:
                raise ValueError(f"{dataset['short_name']} " f"requires a BBOX.")

            minx, miny, maxx, maxy = bbox

            params["bbox"] = (
                f"{minx}," f"{miny}," f"{maxx}," f"{maxy}," f"{dataset['srs_name']}"
            )

        else:
            raise ValueError(f"Unsupported filter type: " f"{filter_type}")

    def count_features(
        self,
        base_url: str,
        dataset: dict[str, Any],
        bbox: (
            tuple[
                float,
                float,
                float,
                float,
            ]
            | None
        ) = None,
    ) -> int | None:
        """
        Gets the matching feature count with resultType=hits.
        """

        params = self._base_getfeature_params(dataset)

        params["resultType"] = "hits"

        # Hits usually returns XML, so remove the GeoJSON format.
        params.pop(
            "outputFormat",
            None,
        )

        self._add_filter(
            params=params,
            dataset=dataset,
            bbox=bbox,
        )

        response = self.session.get(
            base_url,
            params=params,
            timeout=self.timeout_seconds,
        )

        if not response.ok:
            content_type = response.headers.get(
                "Content-Type",
                "",
            )

            raise RuntimeError(
                f"{dataset['short_name']} "
                f"count request failed.\n"
                f"HTTP status: "
                f"{response.status_code}\n"
                f"Content-Type: "
                f"{content_type}\n"
                f"Request URL: "
                f"{redact_url(response.url)}\n"
                f"Response body:\n"
                f"{response.text[:5000]}"
            )

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).lower()

        if "json" in content_type:
            try:
                payload = response.json()

            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"{dataset['short_name']} "
                    f"count response was marked "
                    f"as JSON but could not be parsed.\n"
                    f"Response body:\n"
                    f"{response.text[:5000]}"
                ) from exc

            value = payload.get("numberMatched") or payload.get("totalFeatures")

            if value in (
                None,
                "unknown",
            ):
                return None

            return int(value)

        try:
            root = ET.fromstring(response.content)

        except ET.ParseError as exc:
            raise RuntimeError(
                f"{dataset['short_name']} "
                f"count response could not "
                f"be parsed as XML.\n"
                f"Response body:\n"
                f"{response.text[:5000]}"
            ) from exc

        possible_attributes = (
            "numberMatched",
            "numberOfFeatures",
        )

        for attribute_name in possible_attributes:
            value = root.attrib.get(attribute_name)

            if value and value != "unknown":
                return int(value)

        return None

    def download_pages(
        self,
        base_url: str,
        dataset: dict[str, Any],
        output_dir: Path,
        bbox: (
            tuple[
                float,
                float,
                float,
                float,
            ]
            | None
        ) = None,
    ) -> DownloadResult:
        """
        Downloads paginated WFS GeoJSON files without merging them.
        """

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        expected_count = self.count_features(
            base_url=base_url,
            dataset=dataset,
            bbox=bbox,
        )

        page_size = int(
            dataset.get(
                "page_size",
                5000,
            )
        )

        page_files: list[Path] = []

        request_summaries: list[dict[str, Any]] = []

        downloaded_count = 0
        start_index = 0
        page_number = 1

        while True:
            params = self._base_getfeature_params(dataset)

            params.update(
                {
                    "count": page_size,
                    "startIndex": start_index,
                    "sortBy": str(
                        dataset["sort_by"],
                    ),
                }
            )

            self._add_filter(
                params=params,
                dataset=dataset,
                bbox=bbox,
            )

            started_at = time.monotonic()

            response = self.session.get(
                base_url,
                params=params,
                timeout=self.timeout_seconds,
            )

            if not response.ok:
                content_type = response.headers.get(
                    "Content-Type",
                    "",
                )

                raise RuntimeError(
                    f"{dataset['short_name']} "
                    f"page download failed.\n"
                    f"HTTP status: "
                    f"{response.status_code}\n"
                    f"Content-Type: "
                    f"{content_type}\n"
                    f"Page number: "
                    f"{page_number}\n"
                    f"Start index: "
                    f"{start_index}\n"
                    f"Request URL: "
                    f"{redact_url(response.url)}\n"
                    f"Response body:\n"
                    f"{response.text[:5000]}"
                )

            elapsed_seconds = round(
                time.monotonic() - started_at,
                3,
            )

            try:
                payload = response.json()

            except json.JSONDecodeError as exc:
                response_preview = response.text[:1000]

                raise RuntimeError(
                    f"{dataset['short_name']} "
                    f"returned non-JSON data.\n"
                    f"Page number: "
                    f"{page_number}\n"
                    f"Start index: "
                    f"{start_index}\n"
                    f"Response preview:\n"
                    f"{response_preview}"
                ) from exc

            features = payload.get("features")

            if not isinstance(
                features,
                list,
            ):
                raise RuntimeError(
                    f"{dataset['short_name']} "
                    f"response does not contain "
                    f"a GeoJSON features list.\n"
                    f"Page number: "
                    f"{page_number}\n"
                    f"Start index: "
                    f"{start_index}"
                )

            feature_count = len(features)

            page_file = output_dir / f"page_{page_number:05d}.geojson"

            page_file.write_bytes(response.content)

            page_files.append(page_file)

            request_summaries.append(
                {
                    "page_number": page_number,
                    "start_index": start_index,
                    "sort_by": str(
                        dataset["sort_by"],
                    ),
                    "requested_count": page_size,
                    "returned_count": feature_count,
                    "http_status": response.status_code,
                    "content_type": response.headers.get("Content-Type"),
                    "content_length": len(response.content),
                    "elapsed_seconds": elapsed_seconds,
                    "request_url_redacted": redact_url(response.url),
                }
            )

            downloaded_count += feature_count

            LOGGER.info(
                "%s page=%s returned=%s " "cumulative=%s expected=%s",
                dataset["short_name"],
                page_number,
                feature_count,
                downloaded_count,
                expected_count,
            )

            # Stop and remove the empty file when no features are returned.
            if feature_count == 0:
                page_file.unlink(missing_ok=True)

                page_files.pop()
                request_summaries.pop()

                break

            # Stop when the current page is the last partial page.
            if feature_count < page_size:
                break

            # Stop after reaching the expected feature count.
            if expected_count is not None and downloaded_count >= expected_count:
                break

            start_index += feature_count
            page_number += 1

        return DownloadResult(
            dataset=dataset["short_name"],
            expected_count=expected_count,
            downloaded_count=downloaded_count,
            page_files=page_files,
            request_summaries=request_summaries,
        )
