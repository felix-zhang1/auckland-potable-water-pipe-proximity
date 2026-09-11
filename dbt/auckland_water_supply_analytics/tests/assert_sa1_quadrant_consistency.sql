select *
from {{ ref('mart_sa1_accessibility_quadrants') }}
where
    (
        quadrant_code = 'UNCLASSIFIED'
        and median_pipe_distance_m is not null
        and dwelling_density_per_sq_km is not null
    )
    or
    (
        quadrant_code <> 'UNCLASSIFIED'
        and (
            median_pipe_distance_m is null
            or dwelling_density_per_sq_km is null
        )
    )