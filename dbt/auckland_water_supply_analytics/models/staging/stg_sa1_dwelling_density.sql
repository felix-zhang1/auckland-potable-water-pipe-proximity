with source as (

    select *
    from {{ source('water_supply_raw', 'sa1_dwelling_density') }}

),

latest_snapshot as (

    select
        source_filename
    from source
    group by source_filename
    qualify row_number() over (
        order by max(snowpipe_loaded_at) desc
    ) = 1

),

latest_source as (

    select
        source.*
    from source

    inner join latest_snapshot
        on source.source_filename = latest_snapshot.source_filename

),

typed as (

    select
        sa1_id,
        landwater,
        landwater_name,

        try_to_double(land_area_sq_km)
            as land_area_sq_km,

        try_to_number(occupied_dwellings_2023)
            as occupied_dwellings_2023,

        try_to_double(dwelling_density_per_sq_km)
            as dwelling_density_per_sq_km,

        try_to_timestamp_tz(analysis_created_at)
            as analysis_created_at,

        source_filename,
        source_file_row_number,
        source_file_last_modified,
        snowpipe_loaded_at

    from latest_source

)

select *
from typed