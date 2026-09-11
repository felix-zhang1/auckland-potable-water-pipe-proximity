CREATE SCHEMA IF NOT EXISTS core;
DROP TABLE IF EXISTS core.analysis_boundary CASCADE;
CREATE TABLE core.analysis_boundary AS
SELECT 1::integer AS boundary_id,
    -- Repair invalid source polygons and preserve all resulting polygon parts.
    -- ST_MakeValid may split an invalid Polygon into multiple polygons, so
    -- normalize the final polygonal result to MultiPolygon for a stable
    -- downstream schema, and enforce NZTM2000 (EPSG:2193).
    ST_Multi(
        ST_CollectionExtract(
            ST_MakeValid(geometry),
            3
        )
    )::geometry(MultiPolygon, 2193) AS geometry,
    ingestion_run_id,
    ingestion_date,
    source_provider,
    source_crs,
    loaded_at AS source_loaded_at,
    CURRENT_TIMESTAMP AS transformed_at
FROM staging.urban_rural_2026
WHERE geometry IS NOT NULL
    AND NOT ST_IsEmpty(geometry);
-- Validate the assumptions required by downstream analysis:
-- exactly one boundary row, valid non-empty geometry, and SRID 2193.
-- Fail early if the source dataset changes unexpectedly.
DO $$
DECLARE boundary_count bigint;
invalid_count bigint;
unexpected_srid_count bigint;
BEGIN
SELECT COUNT(*) INTO boundary_count
FROM core.analysis_boundary;
IF boundary_count <> 1 THEN RAISE EXCEPTION 'Expected exactly one analysis boundary, but found % rows.',
boundary_count;
END IF;
SELECT COUNT(*) INTO invalid_count
FROM core.analysis_boundary
WHERE NOT ST_IsValid(geometry)
    OR ST_IsEmpty(geometry);
IF invalid_count <> 0 THEN RAISE EXCEPTION 'Analysis boundary contains invalid or empty geometry.';
END IF;
SELECT COUNT(*) INTO unexpected_srid_count
FROM core.analysis_boundary
WHERE ST_SRID(geometry) <> 2193;
IF unexpected_srid_count <> 0 THEN RAISE EXCEPTION 'Analysis boundary contains an unexpected SRID.';
END IF;
END;
$$;
ALTER TABLE core.analysis_boundary
ADD CONSTRAINT pk_analysis_boundary PRIMARY KEY (boundary_id);
ALTER TABLE core.analysis_boundary
ALTER COLUMN geometry
SET NOT NULL;
CREATE INDEX idx_analysis_boundary_geometry ON core.analysis_boundary USING GIST (geometry);
ANALYZE core.analysis_boundary;