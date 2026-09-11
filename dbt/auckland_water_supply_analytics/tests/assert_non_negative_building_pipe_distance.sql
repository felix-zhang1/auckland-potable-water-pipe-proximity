select *
from {{ ref('stg_building_pipe_proximity') }}
where nearest_pipe_distance_m < 0