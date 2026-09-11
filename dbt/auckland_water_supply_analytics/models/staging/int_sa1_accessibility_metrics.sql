with building_proximity as (

    select *
    from {{ ref('stg_building_pipe_proximity') }}

),

sa1_density as (

    select *
    from {{ ref('stg_sa1_dwelling_density') }}

),

building_proximity_by_sa1 as (

    select
        sa1_id,

        count(*) as building_count,

        avg(nearest_pipe_distance_m)
            as avg_pipe_distance_m,

        median(nearest_pipe_distance_m)
            as median_pipe_distance_m,

        percentile_cont(0.75) within group (
            order by nearest_pipe_distance_m
        ) as p75_pipe_distance_m,

        min(nearest_pipe_distance_m)
            as min_pipe_distance_m,

        max(nearest_pipe_distance_m)
            as max_pipe_distance_m

    from building_proximity

    where sa1_id is not null

    group by sa1_id

),

combined as (

    select
        density.sa1_id,
        density.landwater,
        density.landwater_name,
        density.land_area_sq_km,
        density.occupied_dwellings_2023,
        density.dwelling_density_per_sq_km,

        proximity.building_count,
        proximity.avg_pipe_distance_m,
        proximity.median_pipe_distance_m,
        proximity.p75_pipe_distance_m,
        proximity.min_pipe_distance_m,
        proximity.max_pipe_distance_m,

        density.analysis_created_at
            as dwelling_density_analysis_created_at,

        density.source_filename
            as dwelling_density_source_filename,

        density.snowpipe_loaded_at
            as dwelling_density_loaded_at

    from sa1_density as density

    left join building_proximity_by_sa1 as proximity
        on density.sa1_id = proximity.sa1_id

)

select *
from combined