-- Ensure the staging table has a spatial index
CREATE INDEX IF NOT EXISTS idx_nz_building_outlines_geometry ON staging.nz_building_outlines USING GIST (geometry);
ANALYZE staging.nz_building_outlines;
DROP TABLE IF EXISTS core.nz_building_outlines;
-- Filter source buildings by the analysis boundary, Keep intersecting buildings and preserve full geometries.
CREATE TABLE core.nz_building_outlines AS
SELECT s.building_id,
    s.suburb_locality,
    s.town_city,
    s.territorial_authority,
    s.geometry::geometry(Polygon, 2193) AS geometry,
    -- Create a representative point within each building footprint
    -- for assigning the building to an SA1 in downstream analysis.
    ST_PointOnSurface(s.geometry)::geometry(Point, 2193) AS representative_point,
    s.ingestion_run_id,
    s.ingestion_date,
    s.source_provider,
    s.source_crs,
    s.source_file,
    s.loaded_at AS source_loaded_at,
    CURRENT_TIMESTAMP AS transformed_at
FROM staging.nz_building_outlines AS s
    JOIN core.analysis_boundary AS b ON ST_Intersects(s.geometry, b.geometry)
WHERE s.geometry IS NOT NULL
    AND NOT ST_IsEmpty(s.geometry);
-- Validate row count and geometry quality
DO $$
DECLARE total_count bigint;
invalid_count bigint;
unexpected_srid_count bigint;
unexpected_geometry_type_count bigint;
invalid_point_count bigint;
BEGIN
SELECT COUNT(*),
    COUNT(*) FILTER (
        WHERE geometry IS NULL
            OR ST_IsEmpty(geometry)
            OR NOT ST_IsValid(geometry)
    ),
    COUNT(*) FILTER (
        WHERE ST_SRID(geometry) <> 2193
    ),
    COUNT(*) FILTER (
        WHERE GeometryType(geometry) <> 'POLYGON'
    ),
    COUNT(*) FILTER (
        WHERE representative_point IS NULL
            OR ST_IsEmpty(representative_point)
            OR NOT ST_IsValid(representative_point)
            OR GeometryType(representative_point) <> 'POINT'
            OR ST_SRID(representative_point) <> 2193
    ) INTO total_count,
    invalid_count,
    unexpected_srid_count,
    unexpected_geometry_type_count,
    invalid_point_count
FROM core.nz_building_outlines;
IF total_count = 0 THEN RAISE EXCEPTION 'nz_building_outlines contains no rows.';
END IF;
IF invalid_count <> 0 THEN RAISE EXCEPTION 'nz_building_outlines contains invalid, NULL, or empty geometry. Count=%',
invalid_count;
END IF;
IF unexpected_srid_count <> 0 THEN RAISE EXCEPTION 'nz_building_outlines contains geometry with an unexpected SRID. Count=%',
unexpected_srid_count;
END IF;
IF unexpected_geometry_type_count <> 0 THEN RAISE EXCEPTION 'nz_building_outlines contains an unexpected geometry type. Count=%',
unexpected_geometry_type_count;
END IF;
IF invalid_point_count <> 0 THEN RAISE EXCEPTION 'nz_building_outlines contains invalid representative points. Count=%',
invalid_point_count;
END IF;
END;
$$;
-- Add constraints
ALTER TABLE core.nz_building_outlines
ADD CONSTRAINT pk_nz_building_outlines PRIMARY KEY (building_id);
ALTER TABLE core.nz_building_outlines
ALTER COLUMN geometry
SET NOT NULL;
ALTER TABLE core.nz_building_outlines
ALTER COLUMN representative_point
SET NOT NULL;
-- Create indexes and refresh statistics
CREATE INDEX idx_nz_building_outlines_geometry ON core.nz_building_outlines USING GIST (geometry);
CREATE INDEX idx_nz_building_outlines_representative_point ON core.nz_building_outlines USING GIST (representative_point);
CREATE INDEX idx_nz_building_outlines_town_city ON core.nz_building_outlines (town_city);
CREATE INDEX idx_nz_building_outlines_suburb_locality ON core.nz_building_outlines (suburb_locality);
ANALYZE core.nz_building_outlines;