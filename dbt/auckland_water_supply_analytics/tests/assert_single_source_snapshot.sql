select
    count(distinct source_filename) as snapshot_count
from {{ ref('stg_building_pipe_proximity') }}
having count(distinct source_filename) <> 1