select
    count(distinct source_filename) as snapshot_count
from {{ ref('stg_sa1_dwelling_density') }}
having count(distinct source_filename) <> 1