-- Ensure required spatial and attribute indexes
CREATE INDEX IF NOT EXISTS idx_water_pipe_geometry ON staging.water_pipe USING GIST (geometry);

CREATE INDEX IF NOT EXISTS idx_analysis_boundary_geometry ON core.analysis_boundary USING GIST (geometry);

DROP TABLE IF EXISTS core.water_pipe;

-- Create the core water-pipe table.
-- Retain complete source pipes that intersect the analysis boundary.
CREATE TABLE
    core.water_pipe AS
SELECT
    s.objectid,
    s.gis_id,
    s.compkey,
    s.process,
    s.status,
    s.material,
    s.type,
    s.service,
    s.nom_dia_mm,
    -- Data-lineage attributes.
    s.ingestion_run_id,
    s.ingestion_date,
    s.source_provider,
    s.source_crs,
    s.source_file,
    s.loaded_at AS source_loaded_at,
    CURRENT_TIMESTAMP AS transformed_at,
    -- Enforce the expected LineString geometry type and EPSG:2193 SRID.
    s.geometry::geometry (LineString, 2193) AS geometry
FROM
    staging.water_pipe AS s
WHERE
    s.geometry IS NOT NULL
    AND NOT ST_IsEmpty (s.geometry)
    AND EXISTS (
        SELECT
            1
        FROM
            core.analysis_boundary AS b
        WHERE
            ST_Intersects (b.geometry, s.geometry)
    );

-- Validate the core table
DO $$
DECLARE total_count bigint;
invalid_geometry_count bigint;
unexpected_srid_count bigint;
unexpected_geometry_type_count bigint;
BEGIN
SELECT COUNT(*),
    COUNT(*) FILTER (
        WHERE geometry IS NULL
            OR ST_IsEmpty(geometry)
            OR NOT ST_IsValid(geometry)
    ),
    COUNT(*) FILTER (
        WHERE geometry IS NOT NULL
            AND ST_SRID(geometry) <> 2193
    ),
    COUNT(*) FILTER (
        WHERE geometry IS NOT NULL
            AND ST_GeometryType(geometry) <> 'ST_LineString'
    ) INTO total_count,
    invalid_geometry_count,
    unexpected_srid_count,
    unexpected_geometry_type_count
FROM core.water_pipe;
IF total_count = 0 THEN RAISE EXCEPTION 'core.water_pipe contains no rows.';
END IF;
IF invalid_geometry_count <> 0 THEN RAISE EXCEPTION 'core.water_pipe contains invalid, NULL, or empty geometry. Count=%',
invalid_geometry_count;
END IF;
IF unexpected_srid_count <> 0 THEN RAISE EXCEPTION 'core.water_pipe contains geometry with an unexpected SRID. Count=%',
unexpected_srid_count;
END IF;
IF unexpected_geometry_type_count <> 0 THEN RAISE EXCEPTION 'core.water_pipe contains an unexpected geometry type. Count=%',
unexpected_geometry_type_count;
END IF;
END;
$$;

-- Add core-table constraints
ALTER TABLE core.water_pipe
ADD CONSTRAINT pk_water_pipe PRIMARY KEY (objectid);

ALTER TABLE core.water_pipe
ALTER COLUMN geometry
SET NOT NULL;

ALTER TABLE core.water_pipe
ALTER COLUMN transformed_at
SET NOT NULL;

-- Add table and column comments
COMMENT ON TABLE core.water_pipe IS 'Water-pipe records that intersect core.analysis_boundary. Complete source geometries are retained and are not clipped.';

COMMENT ON COLUMN core.water_pipe.objectid IS 'Source object identifier used as the primary key in the core table.';

COMMENT ON COLUMN core.water_pipe.source_loaded_at IS 'Timestamp when the source record was loaded into the staging layer.';

COMMENT ON COLUMN core.water_pipe.transformed_at IS 'Timestamp when the staging record was transformed into the core layer.';

COMMENT ON COLUMN core.water_pipe.geometry IS 'Complete original water-pipe geometry retained when it intersects the analysis boundary. Geometry is not clipped. Geometry type: LineString; CRS: EPSG:2193.';

-- Create the spatial index.
CREATE INDEX idx_water_pipe_geometry ON core.water_pipe USING GIST (geometry);

ANALYZE core.water_pipe;