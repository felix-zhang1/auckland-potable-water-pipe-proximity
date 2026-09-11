select *
from {{ ref('int_sa1_accessibility_metrics') }}
where median_pipe_distance_m < 0
   or avg_pipe_distance_m < 0
   or p75_pipe_distance_m < 0
   or min_pipe_distance_m < 0
   or max_pipe_distance_m < 0