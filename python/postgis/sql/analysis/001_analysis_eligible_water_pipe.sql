-- Create the PostGIS analysis schema
-- Store deterministic spatial analysis outputs derived from
-- the standardized core-layer datasets.
CREATE SCHEMA IF NOT EXISTS analysis;

COMMENT ON SCHEMA analysis IS 'PostGIS spatial analysis layer containing deterministic water-pipe eligibility and building-to-water-pipe proximity results.';

-- Create the eligible water-pipe network
--
-- Select pipes used in building-to-pipe proximity analysis.
--
-- Eligibility:
-- status  = OP      -- operational pipes
-- service = Local   -- local network
-- type    = Pot     -- potable water
-- process IN ('Principal', 'Rider')  -- local distribution pipes                
--
-- Service Conn pipes are excluded.
-- Full pipe geometries are retained without clipping.
DROP TABLE IF EXISTS analysis.eligible_water_pipe;

-- Create eligible pipe table
CREATE TABLE
  analysis.eligible_water_pipe AS
SELECT
  p.objectid,
  p.gis_id,
  p.compkey,
  p.process,
  p.status,
  p.material,
  p.type,
  p.service,
  p.nom_dia_mm,
  CURRENT_TIMESTAMP AS analysis_created_at,
  p.geometry::geometry (LineString, 2193) AS geometry
FROM
  core.water_pipe AS p
WHERE
  p.status = 'OP'
  AND p.service = 'Local'
  AND p.type = 'Pot'
  AND p.process IN ('Principal', 'Rider')
  AND p.geometry IS NOT NULL
  AND NOT ST_IsEmpty (p.geometry);

-- Validate the eligible network
DO $$
DECLARE total_count bigint;
invalid_geometry_count bigint;
unexpected_srid_count bigint;
unexpected_geometry_type_count bigint;
invalid_business_rule_count bigint;
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
    WHERE ST_GeometryType(geometry) <> 'ST_LineString'
  ),
  COUNT(*) FILTER (
    WHERE status <> 'OP'
      OR service <> 'Local'
      OR type <> 'Pot'
      OR process NOT IN ('Principal', 'Rider')
  ) INTO total_count,
  invalid_geometry_count,
  unexpected_srid_count,
  unexpected_geometry_type_count,
  invalid_business_rule_count
FROM analysis.eligible_water_pipe;
IF total_count = 0 THEN RAISE EXCEPTION 'analysis.eligible_water_pipe contains no rows.';
END IF;
IF invalid_geometry_count <> 0 THEN RAISE EXCEPTION 'Eligible water-pipe table contains invalid, NULL, or empty geometry. Count=%',
invalid_geometry_count;
END IF;
IF unexpected_srid_count <> 0 THEN RAISE EXCEPTION 'Eligible water-pipe table contains an unexpected SRID. Count=%',
unexpected_srid_count;
END IF;
IF unexpected_geometry_type_count <> 0 THEN RAISE EXCEPTION 'Eligible water-pipe table contains an unexpected geometry type. Count=%',
unexpected_geometry_type_count;
END IF;
IF invalid_business_rule_count <> 0 THEN RAISE EXCEPTION 'Eligible water-pipe table contains rows that violate the eligibility rules. Count=%',
invalid_business_rule_count;
END IF;
END;
$$;

-- Add constraints
ALTER TABLE analysis.eligible_water_pipe
ADD CONSTRAINT pk_eligible_water_pipe PRIMARY KEY (objectid),
ALTER COLUMN geometry
SET NOT NULL,
ALTER COLUMN analysis_created_at
SET NOT NULL,
ADD CONSTRAINT chk_eligible_water_pipe_status CHECK (status = 'OP'),
ADD CONSTRAINT chk_eligible_water_pipe_service CHECK (service = 'Local'),
ADD CONSTRAINT chk_eligible_water_pipe_type CHECK (
  type = 'Pot'
),
ADD CONSTRAINT chk_eligible_water_pipe_process CHECK (process IN ('Principal', 'Rider'));

-- Create spatial index
CREATE INDEX idx_eligible_water_pipe_geometry ON analysis.eligible_water_pipe USING GIST (geometry);

-- Documentation
COMMENT ON TABLE analysis.eligible_water_pipe IS 'Operational local potable Principal and Rider water pipes used as the eligible network for deterministic building-to-pipe proximity analysis. Service connections are excluded.';

COMMENT ON COLUMN analysis.eligible_water_pipe.geometry IS 'Complete eligible water-pipe geometry in NZTM2000 (EPSG:2193).';

-- Update planner statistics for downstream nearest-neighbour search.
ANALYZE analysis.eligible_water_pipe;