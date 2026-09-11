select *
from {{ ref('stg_sa1_dwelling_density') }}
where land_area_sq_km <= 0