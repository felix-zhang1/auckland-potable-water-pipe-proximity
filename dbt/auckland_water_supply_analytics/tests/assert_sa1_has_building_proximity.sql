select *
from {{ ref('int_sa1_accessibility_metrics') }}
where
    (
        building_count is not null
        and (
            avg_pipe_distance_m is null
            or median_pipe_distance_m is null
            or p75_pipe_distance_m is null
        )
    )
    or
    (
        building_count is null
        and (
            avg_pipe_distance_m is not null
            or median_pipe_distance_m is not null
            or p75_pipe_distance_m is not null
        )
    )