with source as (

    select *
    from {{ source('water_supply_raw', 'building_pipe_proximity') }}

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
        building_id,
        suburb_locality,
        town_city,
        territorial_authority,
        sa1_id,

        try_to_number(nearest_pipe_objectid)
            as nearest_pipe_objectid,

        nearest_pipe_gis_id,
        nearest_pipe_compkey,
        nearest_pipe_process,
        nearest_pipe_material,

        try_to_number(nearest_pipe_nom_dia_mm)
            as nearest_pipe_nom_dia_mm,

        try_to_double(nearest_pipe_distance_m)
            as nearest_pipe_distance_m,

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