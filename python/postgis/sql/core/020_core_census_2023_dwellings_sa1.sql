DROP TABLE IF EXISTS core.census_2023_dwellings_sa1;

-- Create the new core Census table
--
--- Spatial selection rule:
-- Retain a complete land-based SA1 polygon when its representative
-- point is covered by the Auckland urban analysis boundary.
--
-- ST_PointOnSurface is used to generate a representative point
-- guaranteed to lie on/in the SA1 polygon.
-- ST_Covers is then used as the spatial selection predicate.
--
-- The original SA1 geometry is preserved and is not clipped.
--
-- Land/water filtering:
-- 11 = Island       -> retained
-- 12 = Mainland     -> retained
-- 21 = Inland Water -> excluded
-- 22 = Inlet        -> excluded
-- 23 = Oceanic      -> excluded
--
-- Census variable mapping:
-- VAR_3_54 -> occupied_dwellings_2023
--
-- VAR_3_54:
-- Year: 2023
-- Measure: Count
-- Variable: Dwelling occupancy status
-- Category: Occupied Dwelling
--
-- Special-value handling:
-- -999 is not a real dwelling count and is assigned to null.
-- Zero remains a valid occupied-dwelling count.
CREATE TABLE
    core.census_2023_dwellings_sa1 AS
WITH
    prepared AS (
        SELECT
            s.sa12023_v1_00,
            s.landwater,
            s.landwater_name,
            s.var_3_54,
            s.land_area_sq_km,
            -- Create a representative point guaranteed to lie
            -- on/in the SA1 polygon for spatial assignment.
            ST_PointOnSurface (s.geometry)::geometry (Point, 2193) AS representative_point,
            s.geometry
        FROM
            staging.census_2023_dwellings_sa1 AS s
        WHERE
            s.geometry IS NOT NULL
            AND NOT ST_IsEmpty (s.geometry)
            AND s.landwater IN ('11', '12')
    )
SELECT
    p.sa12023_v1_00 AS sa1_id,
    p.landwater,
    p.landwater_name,
    p.land_area_sq_km,
    -- -999 represents an unavailable/special Census value.
    -- Preserve genuine zero counts.
    NULLIF(p.var_3_54, -999)::integer AS occupied_dwellings_2023,
    CURRENT_TIMESTAMP AS transformed_at,
    -- validate whether the geometry type is MultiPolygon, 
    -- because the geometry type of staging.census_2023_dwellings_sa1 is MultiPolygon.
    p.geometry::geometry (MultiPolygon, 2193) AS geometry
FROM
    prepared AS p -- Assign the complete SA1 to the analysis area using a
    -- representative point rather than any polygon intersection.
WHERE
    EXISTS (
        SELECT
            1
        FROM
            core.analysis_boundary AS b
        WHERE
            ST_Covers (b.geometry, p.representative_point)
    );

-- Validate the new table
DO $$
DECLARE total_count bigint;
invalid_geometry_count bigint;
unexpected_srid_count bigint;
unexpected_geometry_type_count bigint;
invalid_landwater_count bigint;
negative_occupied_dwellings_count bigint;
missing_occupied_dwellings_count bigint;
invalid_land_area_count bigint;
invalid_boundary_selection_count bigint;
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
        WHERE ST_GeometryType(geometry) <> 'ST_MultiPolygon'
    ),
    COUNT(*) FILTER (
        WHERE landwater NOT IN ('11', '12')
    ),
    COUNT(*) FILTER (
        WHERE occupied_dwellings_2023 < 0
    ),
    COUNT(*) FILTER (
        WHERE occupied_dwellings_2023 IS NULL
    ),
    COUNT(*) FILTER (
    WHERE land_area_sq_km IS NULL
        OR land_area_sq_km <= 0
),
    COUNT(*) FILTER (
        WHERE NOT EXISTS (
                SELECT 1
                FROM core.analysis_boundary AS b
                WHERE ST_Intersects(
                        b.geometry,
                        c.geometry
                    )
            )
    ) INTO total_count,
    invalid_geometry_count,
    unexpected_srid_count,
    unexpected_geometry_type_count,
    invalid_landwater_count,
    negative_occupied_dwellings_count,
    missing_occupied_dwellings_count,
    invalid_land_area_count,
    invalid_boundary_selection_count
FROM core.census_2023_dwellings_sa1 AS c;
IF total_count = 0 THEN RAISE EXCEPTION 'census_2023_dwellings_sa1 contains no rows.';
END IF;
IF invalid_geometry_count <> 0 THEN RAISE EXCEPTION 'census_2023_dwellings_sa1 contains invalid, NULL, or empty geometry. Count=%',
invalid_geometry_count;
END IF;
IF unexpected_srid_count <> 0 THEN RAISE EXCEPTION 'census_2023_dwellings_sa1 contains an unexpected SRID. Count=%',
unexpected_srid_count;
END IF;
IF unexpected_geometry_type_count <> 0 THEN RAISE EXCEPTION 'census_2023_dwellings_sa1 contains an unexpected geometry type. Count=%',
unexpected_geometry_type_count;
END IF;
IF invalid_landwater_count <> 0 THEN RAISE EXCEPTION 'census_2023_dwellings_sa1 contains non-land SA1 records. Count=%',
invalid_landwater_count;
END IF;
IF negative_occupied_dwellings_count <> 0 THEN RAISE EXCEPTION 'census_2023_dwellings_sa1 contains negative occupied dwelling counts. Count=%',
negative_occupied_dwellings_count;
END IF;
IF missing_occupied_dwellings_count <> 0 THEN RAISE NOTICE 'SA1 records with unavailable occupied-dwelling counts=%',
missing_occupied_dwellings_count;
END IF;
IF invalid_land_area_count <> 0 THEN
    RAISE EXCEPTION
        'census_2023_dwellings_sa1 contains invalid land-area values (NULL or <= 0). Count=%',
        invalid_land_area_count;
END IF;
IF invalid_boundary_selection_count <> 0 THEN RAISE EXCEPTION 'census_2023_dwellings_sa1 contains SA1 records that do not intersect the Auckland urban analysis boundary. Count=%',
invalid_boundary_selection_count;
END IF;
END;
$$;

-- Add constraints
ALTER TABLE core.census_2023_dwellings_sa1
ADD CONSTRAINT pk_census_2023_dwellings_sa1 PRIMARY KEY (sa1_id);

ALTER TABLE core.census_2023_dwellings_sa1
ALTER COLUMN landwater
SET NOT NULL;

ALTER TABLE core.census_2023_dwellings_sa1
ALTER COLUMN landwater_name
SET NOT NULL;

ALTER TABLE core.census_2023_dwellings_sa1
ALTER COLUMN transformed_at
SET NOT NULL;

ALTER TABLE core.census_2023_dwellings_sa1
ALTER COLUMN geometry
SET NOT NULL;

ALTER TABLE core.census_2023_dwellings_sa1
ALTER COLUMN land_area_sq_km
SET NOT NULL;

ALTER TABLE core.census_2023_dwellings_sa1
ADD CONSTRAINT chk_census_land_area_positive CHECK (land_area_sq_km > 0);

-- Allow only land-based SA1 classifications.
ALTER TABLE core.census_2023_dwellings_sa1
ADD CONSTRAINT chk_census_landwater CHECK (landwater IN ('11', '12'));

-- Prevent negative occupied-dwelling counts.
ALTER TABLE core.census_2023_dwellings_sa1
ADD CONSTRAINT chk_census_occupied_dwellings CHECK (occupied_dwellings_2023 >= 0);

-- Add table and column documentation
COMMENT ON TABLE core.census_2023_dwellings_sa1 IS 'Stats NZ 2023 Census SA1 occupied-dwelling data for the Auckland urban analysis area. A complete land-based SA1 multipolygon is retained when a representative point generated using ST_PointOnSurface is covered by core.analysis_boundary. Island and Mainland SA1 records are retained. Source VAR_3_54 value -999 is represented as NULL, while zero is retained as a valid occupied-dwelling count.';

COMMENT ON COLUMN core.census_2023_dwellings_sa1.sa1_id IS 'Unique Statistical Area 1 identifier for the Stats NZ 2023 geographic standard.';

COMMENT ON COLUMN core.census_2023_dwellings_sa1.landwater IS 'Stats NZ LANDWATER classification code. Core records are limited to 11 (Island) and 12 (Mainland).';

COMMENT ON COLUMN core.census_2023_dwellings_sa1.landwater_name IS 'Stats NZ LANDWATER classification name. Core records are limited to Island and Mainland.';

COMMENT ON COLUMN core.census_2023_dwellings_sa1.occupied_dwellings_2023 IS 'Number of occupied dwellings in the complete SA1 according to the 2023 Census. Source field: VAR_3_54. Source special value -999 is represented as NULL; zero is retained as a valid count.';

COMMENT ON COLUMN core.census_2023_dwellings_sa1.geometry IS 'Complete SA1 geometry retained when its representative point is covered by the Auckland urban analysis boundary; not clipped. MultiPolygon, EPSG:2193.';

COMMENT ON COLUMN core.census_2023_dwellings_sa1.land_area_sq_km IS 'Land area of the complete SA1 in square kilometres, sourced from staging.census_2023_dwellings_sa1.';

-- Create indexes
CREATE INDEX idx_census_2023_dwellings_sa1_geometry ON core.census_2023_dwellings_sa1 USING GIST (geometry);

ANALYZE core.census_2023_dwellings_sa1;