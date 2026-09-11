select *
from {{ ref('stg_sa1_dwelling_density') }}
where dwelling_density_per_sq_km < 0