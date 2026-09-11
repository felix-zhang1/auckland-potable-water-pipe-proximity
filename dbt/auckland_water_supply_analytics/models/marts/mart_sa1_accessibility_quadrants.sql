with sa1_metrics as (

    select *
    from {{ ref('int_sa1_accessibility_metrics') }}

),

classifiable_sa1 as (

    select *
    from sa1_metrics
    where median_pipe_distance_m is not null
      and dwelling_density_per_sq_km is not null

),

thresholds as (

    select
        median(median_pipe_distance_m)
            as median_pipe_distance_threshold_m,

        percentile_cont(0.75) within group (
            order by median_pipe_distance_m
        ) as p75_pipe_distance_threshold_m,

        median(dwelling_density_per_sq_km)
            as median_dwelling_density_threshold,

        percentile_cont(0.75) within group (
            order by dwelling_density_per_sq_km
        ) as p75_dwelling_density_threshold

    from classifiable_sa1

),

classified as (

    select
        metrics.sa1_id,
        metrics.landwater,
        metrics.landwater_name,
        metrics.land_area_sq_km,
        metrics.occupied_dwellings_2023,
        metrics.dwelling_density_per_sq_km,

        metrics.building_count,
        metrics.avg_pipe_distance_m,
        metrics.median_pipe_distance_m,
        metrics.p75_pipe_distance_m,
        metrics.min_pipe_distance_m,
        metrics.max_pipe_distance_m,

        thresholds.median_pipe_distance_threshold_m,
        thresholds.p75_pipe_distance_threshold_m,
        thresholds.median_dwelling_density_threshold,
        thresholds.p75_dwelling_density_threshold,

        case

            when metrics.median_pipe_distance_m is null
                or metrics.dwelling_density_per_sq_km is null
                then 'UNCLASSIFIED'

            when metrics.median_pipe_distance_m
                    <= thresholds.median_pipe_distance_threshold_m
                and metrics.dwelling_density_per_sq_km
                    <= thresholds.median_dwelling_density_threshold
                then 'Q1'

            when metrics.median_pipe_distance_m
                    <= thresholds.median_pipe_distance_threshold_m
                and metrics.dwelling_density_per_sq_km
                    > thresholds.median_dwelling_density_threshold
                then 'Q2'

            when metrics.median_pipe_distance_m
                    > thresholds.median_pipe_distance_threshold_m
                and metrics.dwelling_density_per_sq_km
                    <= thresholds.median_dwelling_density_threshold
                then 'Q3'

            when metrics.median_pipe_distance_m
                    > thresholds.median_pipe_distance_threshold_m
                and metrics.dwelling_density_per_sq_km
                    > thresholds.median_dwelling_density_threshold
                then 'Q4'

        end as quadrant_code,

        case

            when metrics.median_pipe_distance_m is null
                or metrics.dwelling_density_per_sq_km is null
                then 'Insufficient data'

            when metrics.median_pipe_distance_m
                    <= thresholds.median_pipe_distance_threshold_m
                and metrics.dwelling_density_per_sq_km
                    <= thresholds.median_dwelling_density_threshold
                then 'Lower density / better pipe proximity'

            when metrics.median_pipe_distance_m
                    <= thresholds.median_pipe_distance_threshold_m
                and metrics.dwelling_density_per_sq_km
                    > thresholds.median_dwelling_density_threshold
                then 'Higher density / better pipe proximity'

            when metrics.median_pipe_distance_m
                    > thresholds.median_pipe_distance_threshold_m
                and metrics.dwelling_density_per_sq_km
                    <= thresholds.median_dwelling_density_threshold
                then 'Lower density / poorer pipe proximity'

            when metrics.median_pipe_distance_m
                    > thresholds.median_pipe_distance_threshold_m
                and metrics.dwelling_density_per_sq_km
                    > thresholds.median_dwelling_density_threshold
                then 'Higher density / poorer pipe proximity'

        end as quadrant_name,

        case
            when metrics.median_pipe_distance_m is null
                then null

            when metrics.median_pipe_distance_m
                    >= thresholds.p75_pipe_distance_threshold_m
                then true

            else false
        end as is_extreme_pipe_distance,

        case
            when metrics.dwelling_density_per_sq_km is null
                then null

            when metrics.dwelling_density_per_sq_km
                    >= thresholds.p75_dwelling_density_threshold
                then true

            else false
        end as is_extreme_dwelling_density,

        case
            when metrics.median_pipe_distance_m is null
                or metrics.dwelling_density_per_sq_km is null
                then null

            when metrics.median_pipe_distance_m
                    > thresholds.median_pipe_distance_threshold_m
                and metrics.dwelling_density_per_sq_km
                    > thresholds.median_dwelling_density_threshold
                then true

            else false
        end as is_priority_quadrant,

        metrics.dwelling_density_analysis_created_at,
        metrics.dwelling_density_source_filename,
        metrics.dwelling_density_loaded_at

    from sa1_metrics as metrics

    cross join thresholds

)

select *
from classified