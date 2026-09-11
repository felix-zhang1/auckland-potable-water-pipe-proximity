-- Calculate occupied-dwelling density for each Census SA1.
--
-- Density formula:
-- occupied_dwellings_2023 / land_area_sq_km
--
-- Unit:
-- occupied dwellings per square kilometre.
--
-- If occupied_dwellings_2023 is NULL because the source Census
-- value was unavailable, dwelling_density_per_sq_km is also NULL.
DROP TABLE IF EXISTS analysis.sa1_dwelling_density;

-- Create SA1-level dwelling-density analysis table
CREATE TABLE
  analysis.sa1_dwelling_density AS
SELECT
  c.sa1_id,
  c.landwater,
  c.landwater_name,
  c.land_area_sq_km,
  c.occupied_dwellings_2023,
  (
    c.occupied_dwellings_2023::double precision / c.land_area_sq_km
  ) AS dwelling_density_per_sq_km,
  CURRENT_TIMESTAMP AS analysis_created_at,
  c.geometry::geometry (MultiPolygon, 2193) AS geometry
FROM
  core.census_2023_dwellings_sa1 AS c;

-- Validate SA1 dwelling-density result
DO $$
DECLARE
    source_sa1_count bigint;
    result_count bigint;
    duplicate_sa1_count bigint;
    invalid_land_area_count bigint;
    invalid_density_count bigint;
    missing_density_count bigint;
    invalid_geometry_count bigint;

BEGIN

    SELECT COUNT(*)
    INTO source_sa1_count
    FROM core.census_2023_dwellings_sa1;


    SELECT
        COUNT(*),

        COUNT(*) - COUNT(DISTINCT sa1_id),

        COUNT(*) FILTER (
            WHERE land_area_sq_km IS NULL
               OR land_area_sq_km <= 0
        ),

        COUNT(*) FILTER (
            WHERE dwelling_density_per_sq_km < 0
        ),

        COUNT(*) FILTER (
            WHERE dwelling_density_per_sq_km IS NULL
        ),

        COUNT(*) FILTER (
            WHERE geometry IS NULL
               OR ST_IsEmpty(geometry)
               OR NOT ST_IsValid(geometry)
               OR ST_SRID(geometry) <> 2193
               OR ST_GeometryType(geometry) <> 'ST_MultiPolygon'
        )

    INTO
        result_count,
        duplicate_sa1_count,
        invalid_land_area_count,
        invalid_density_count,
        missing_density_count,
        invalid_geometry_count

    FROM analysis.sa1_dwelling_density;


    -- Every core SA1 should have exactly one analysis record.
    IF result_count <> source_sa1_count THEN
        RAISE EXCEPTION
            'SA1 dwelling-density row-count mismatch. Core SA1 rows=%, analysis rows=%',
            source_sa1_count,
            result_count;
    END IF;


    -- sa1_id must remain unique.
    IF duplicate_sa1_count <> 0 THEN
        RAISE EXCEPTION
            'Duplicate SA1 IDs found in dwelling-density result. Count=%',
            duplicate_sa1_count;
    END IF;


    -- Land area must remain positive.
    IF invalid_land_area_count <> 0 THEN
        RAISE EXCEPTION
            'Invalid land-area values found in dwelling-density result (NULL or <= 0). Count=%',
            invalid_land_area_count;
    END IF;


    -- Dwelling density cannot be negative.
    IF invalid_density_count <> 0 THEN
        RAISE EXCEPTION
            'Negative dwelling-density values found. Count=%',
            invalid_density_count;
    END IF;


    -- NULL density is allowed when the source occupied-dwelling
    -- Census value is unavailable.
    IF missing_density_count <> 0 THEN
        RAISE NOTICE
            'SA1 records with unavailable dwelling density: %',
            missing_density_count;
    END IF;


    IF invalid_geometry_count <> 0 THEN
        RAISE EXCEPTION
            'Invalid SA1 geometries found in dwelling-density result. Count=%',
            invalid_geometry_count;
    END IF;

END;
$$;

-- Add constraints
ALTER TABLE analysis.sa1_dwelling_density
ADD CONSTRAINT pk_sa1_dwelling_density PRIMARY KEY (sa1_id);

ALTER TABLE analysis.sa1_dwelling_density
ALTER COLUMN landwater
SET NOT NULL;

ALTER TABLE analysis.sa1_dwelling_density
ALTER COLUMN landwater_name
SET NOT NULL;

ALTER TABLE analysis.sa1_dwelling_density
ALTER COLUMN land_area_sq_km
SET NOT NULL;

ALTER TABLE analysis.sa1_dwelling_density
ALTER COLUMN analysis_created_at
SET NOT NULL;

ALTER TABLE analysis.sa1_dwelling_density
ALTER COLUMN geometry
SET NOT NULL;

ALTER TABLE analysis.sa1_dwelling_density
ADD CONSTRAINT chk_sa1_dwelling_density_land_area CHECK (land_area_sq_km > 0);

ALTER TABLE analysis.sa1_dwelling_density
ADD CONSTRAINT chk_sa1_dwelling_density_non_negative CHECK (dwelling_density_per_sq_km >= 0);

-- Create indexes
CREATE INDEX idx_sa1_dwelling_density_density ON analysis.sa1_dwelling_density (dwelling_density_per_sq_km);

CREATE INDEX idx_sa1_dwelling_density_geometry ON analysis.sa1_dwelling_density USING GIST (geometry);

-- Documentation
COMMENT ON TABLE analysis.sa1_dwelling_density IS 'SA1-level occupied-dwelling density analysis for the Auckland urban analysis area. Density is calculated as occupied dwellings divided by SA1 land area in square kilometres.';

COMMENT ON COLUMN analysis.sa1_dwelling_density.sa1_id IS 'Unique Statistical Area 1 identifier for the Stats NZ 2023 geographic standard.';

COMMENT ON COLUMN analysis.sa1_dwelling_density.land_area_sq_km IS 'Land area of the complete SA1 in square kilometres, inherited from core.census_2023_dwellings_sa1.';

COMMENT ON COLUMN analysis.sa1_dwelling_density.occupied_dwellings_2023 IS 'Number of occupied dwellings in the SA1 according to the 2023 Census. NULL indicates an unavailable Census value.';

COMMENT ON COLUMN analysis.sa1_dwelling_density.dwelling_density_per_sq_km IS 'Occupied-dwelling density calculated as occupied_dwellings_2023 divided by land_area_sq_km. Unit: occupied dwellings per square kilometre. NULL when occupied_dwellings_2023 is unavailable.';

COMMENT ON COLUMN analysis.sa1_dwelling_density.geometry IS 'Complete SA1 MultiPolygon geometry inherited from core.census_2023_dwellings_sa1. EPSG:2193.';

-- Update planner statistics
ANALYZE analysis.sa1_dwelling_density;