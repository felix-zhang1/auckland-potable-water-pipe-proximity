from __future__ import annotations

from pathlib import Path

import geopandas as gpd


def calculate_total_bounds(
    page_files: list[Path],
    expected_crs: str,
) -> tuple[float, float, float, float]:
    """
    Calculate the combined BBOX of multiple GeoJSON files.

    Returns:
        minx, miny, maxx, maxy
    """

    if not page_files:
        raise ValueError(
            "Cannot calculate a BBOX because no page files were downloaded."
        )

    all_bounds: list[
        tuple[
            float,
            float,
            float,
            float,
        ]
    ] = []

    for page_file in page_files:
        gdf = gpd.read_file(
            page_file,
            engine="pyogrio",
        )

        if gdf.empty:
            continue

        if gdf.crs is None:
            raise ValueError(f"Missing CRS in file: {page_file}")

        actual_crs = gdf.crs.to_string().upper()
        target_crs = expected_crs.upper()

        if actual_crs != target_crs:
            gdf = gdf.to_crs(expected_crs)

        minx, miny, maxx, maxy = gdf.total_bounds

        all_bounds.append(
            (
                float(minx),
                float(miny),
                float(maxx),
                float(maxy),
            )
        )

    if not all_bounds:
        raise ValueError("All BBOX source pages were empty.")

    overall_minx = min(bounds[0] for bounds in all_bounds)

    overall_miny = min(bounds[1] for bounds in all_bounds)

    overall_maxx = max(bounds[2] for bounds in all_bounds)

    overall_maxy = max(bounds[3] for bounds in all_bounds)

    return (
        overall_minx,
        overall_miny,
        overall_maxx,
        overall_maxy,
    )
