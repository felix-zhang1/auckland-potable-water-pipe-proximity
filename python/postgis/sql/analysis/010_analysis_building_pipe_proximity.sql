-- Calculate building-to-water-pipe proximity
--
-- Spatial logic:
--
-- 1. Assign each building to an SA1 using:
--    ST_Covers(SA1 geometry, building representative point).
--    The representative point is used only for SA1 assignment.
--
-- 2. Find the nearest eligible water pipe using a
--    GiST-assisted nearest-neighbour search.
--
-- 3. Calculate the shortest distance between the full building
--    footprint and full pipe geometry using ST_Distance().
--
-- Both geometries use EPSG:2193, so distance is in metres.
DROP TABLE IF EXISTS analysis.building_pipe_proximity;

-- Create building-level spatial proximity result
CREATE TABLE
  analysis.building_pipe_proximity AS
WITH
  building_sa1 AS (
    -- Assign each building to a Census SA1
    SELECT
      b.building_id,
      b.suburb_locality,
      b.town_city,
      b.territorial_authority,
      b.representative_point,
      b.geometry,
      sa1.sa1_id
    FROM
      core.nz_building_outlines AS b
      LEFT JOIN LATERAL (
        SELECT
          c.sa1_id
        FROM
          core.census_2023_dwellings_sa1 AS c
        WHERE
          ST_Covers (c.geometry, b.representative_point)
          -- Normally one SA1 should cover the representative point.
          -- Ordering provides deterministic handling of overlaps.
        ORDER BY
          c.sa1_id
        LIMIT
          1
      ) AS sa1 ON TRUE
  ),
  building_nearest_pipe AS (
    -- Find the nearest eligible water pipe for each building
    SELECT
      b.building_id,
      b.suburb_locality,
      b.town_city,
      b.territorial_authority,
      b.representative_point,
      b.geometry,
      b.sa1_id,
      nearest_pipe.objectid AS nearest_pipe_objectid,
      nearest_pipe.gis_id AS nearest_pipe_gis_id,
      nearest_pipe.compkey AS nearest_pipe_compkey,
      nearest_pipe.process AS nearest_pipe_process,
      nearest_pipe.material AS nearest_pipe_material,
      nearest_pipe.nom_dia_mm AS nearest_pipe_nom_dia_mm,
      nearest_pipe.distance_m AS nearest_pipe_distance_m
    FROM
      building_sa1 AS b
      LEFT JOIN LATERAL (
        SELECT
          p.objectid,
          p.gis_id,
          p.compkey,
          p.process,
          p.material,
          p.nom_dia_mm,
          ST_Distance (b.geometry, p.geometry)::double precision AS distance_m
        FROM
          analysis.eligible_water_pipe AS p
        ORDER BY
          -- Use KNN ordering to return the nearest pipe.
          p.geometry <-> b.geometry
        LIMIT
          1
      ) AS nearest_pipe ON TRUE
  )
SELECT
  building_id,
  suburb_locality,
  town_city,
  territorial_authority,
  sa1_id,
  nearest_pipe_objectid,
  nearest_pipe_gis_id,
  nearest_pipe_compkey,
  nearest_pipe_process,
  nearest_pipe_material,
  nearest_pipe_nom_dia_mm,
  nearest_pipe_distance_m,
  CURRENT_TIMESTAMP AS analysis_created_at,
  representative_point::geometry (Point, 2193) AS building_representative_point,
  geometry::geometry (Polygon, 2193) AS building_geometry
FROM
  building_nearest_pipe;

-- Validate building-level result
DO $$
DECLARE
    source_building_count bigint;
    result_count bigint;
    duplicate_building_count bigint;
    missing_nearest_pipe_count bigint;
    invalid_distance_count bigint;
    invalid_building_geometry_count bigint;
    invalid_point_count bigint;
    unassigned_sa1_count bigint;

BEGIN

    SELECT COUNT(*)
    INTO source_building_count
    FROM core.nz_building_outlines;


    SELECT
        COUNT(*),

        COUNT(*) - COUNT(DISTINCT building_id),

        COUNT(*) FILTER (
            WHERE nearest_pipe_objectid IS NULL
        ),

        COUNT(*) FILTER (
            WHERE nearest_pipe_distance_m IS NULL
               OR nearest_pipe_distance_m < 0
        ),

        COUNT(*) FILTER (
            WHERE building_geometry IS NULL
               OR ST_IsEmpty(building_geometry)
               OR NOT ST_IsValid(building_geometry)
               OR ST_SRID(building_geometry) <> 2193
               OR ST_GeometryType(building_geometry) <> 'ST_Polygon'
        ),

        COUNT(*) FILTER (
            WHERE building_representative_point IS NULL
               OR ST_IsEmpty(building_representative_point)
               OR NOT ST_IsValid(building_representative_point)
               OR ST_SRID(building_representative_point) <> 2193
               OR ST_GeometryType(building_representative_point) <> 'ST_Point'
        ),

        COUNT(*) FILTER (
            WHERE sa1_id IS NULL
        )

    INTO
        result_count,
        duplicate_building_count,
        missing_nearest_pipe_count,
        invalid_distance_count,
        invalid_building_geometry_count,
        invalid_point_count,
        unassigned_sa1_count

    FROM analysis.building_pipe_proximity;


    -- Every core building should have one proximity record.
    IF result_count <> source_building_count THEN
        RAISE EXCEPTION
            'Building proximity row-count mismatch. Core buildings=%, analysis rows=%',
            source_building_count,
            result_count;
    END IF;


    -- building_id must remain unique.
    IF duplicate_building_count <> 0 THEN
        RAISE EXCEPTION
            'Duplicate building IDs found in building proximity result. Count=%',
            duplicate_building_count;
    END IF;


    -- The eligible network is expected to be non-empty, so every
    -- building should obtain a nearest eligible pipe.
    IF missing_nearest_pipe_count <> 0 THEN
        RAISE EXCEPTION
            'Buildings without a nearest eligible water pipe found. Count=%',
            missing_nearest_pipe_count;
    END IF;


    -- Distance must be non-negative.
    IF invalid_distance_count <> 0 THEN
        RAISE EXCEPTION
            'NULL or negative building-to-pipe distances found. Count=%',
            invalid_distance_count;
    END IF;


    IF invalid_building_geometry_count <> 0 THEN
        RAISE EXCEPTION
            'Invalid building geometries found in the analysis result. Count=%',
            invalid_building_geometry_count;
    END IF;


    IF invalid_point_count <> 0 THEN
        RAISE EXCEPTION
            'Invalid building representative points found in the analysis result. Count=%',
            invalid_point_count;
    END IF;


    -- An unmatched SA1 does not invalidate the building-to-pipe
    -- distance, so report it rather than failing the analysis.
    IF unassigned_sa1_count <> 0 THEN
    RAISE NOTICE
        'Buildings without an assigned SA1: %',
        unassigned_sa1_count;
    END IF;

END;
$$;

-- Add constraints
ALTER TABLE analysis.building_pipe_proximity
ADD CONSTRAINT pk_building_pipe_proximity PRIMARY KEY (building_id),
ALTER COLUMN nearest_pipe_objectid
SET NOT NULL,
ALTER COLUMN nearest_pipe_distance_m
SET NOT NULL,
ALTER COLUMN analysis_created_at
SET NOT NULL,
ALTER COLUMN building_representative_point
SET NOT NULL,
ALTER COLUMN building_geometry
SET NOT NULL,
ADD CONSTRAINT chk_building_pipe_distance CHECK (nearest_pipe_distance_m >= 0);

-- Create attribute indexes
-- Supports downstream grouping and filtering by Census SA1.
CREATE INDEX idx_building_pipe_proximity_sa1 ON analysis.building_pipe_proximity (sa1_id);

-- Supports inspection of buildings sharing the same nearest pipe.
CREATE INDEX idx_building_pipe_proximity_nearest_pipe ON analysis.building_pipe_proximity (nearest_pipe_objectid);

-- Supports QA and exploratory distance queries.
CREATE INDEX idx_building_pipe_proximity_distance ON analysis.building_pipe_proximity (nearest_pipe_distance_m);

-- Create spatial indexes
--
-- These are for downstream PostGIS/QGIS spatial queries.
-- They are not required for the nearest-pipe KNN search above.
CREATE INDEX idx_building_pipe_proximity_point ON analysis.building_pipe_proximity USING GIST (building_representative_point);

CREATE INDEX idx_building_pipe_proximity_geometry ON analysis.building_pipe_proximity USING GIST (building_geometry);

-- Documentation
COMMENT ON TABLE analysis.building_pipe_proximity IS 'One deterministic spatial-analysis record per Auckland urban building, containing its assigned Census SA1, nearest eligible water pipe, and exact shortest building-footprint-to-pipe distance.';

COMMENT ON COLUMN analysis.building_pipe_proximity.sa1_id IS 'Census SA1 assigned using the building representative point and ST_Covers. NULL indicates that no core Census SA1 covered the representative point.';

COMMENT ON COLUMN analysis.building_pipe_proximity.nearest_pipe_distance_m IS 'Exact shortest 2D distance in metres between the complete building footprint and the complete nearest eligible water-pipe geometry in EPSG:2193. No threshold classification is applied in PostGIS.';

COMMENT ON COLUMN analysis.building_pipe_proximity.building_representative_point IS 'Representative point inherited from core.nz_building_outlines and used for Census SA1 assignment only. It is not used to calculate water-pipe distance.';

COMMENT ON COLUMN analysis.building_pipe_proximity.building_geometry IS 'Complete building footprint used to calculate nearest_pipe_distance_m.';

-- Update planner statistics for downstream queries.
ANALYZE analysis.building_pipe_proximity;