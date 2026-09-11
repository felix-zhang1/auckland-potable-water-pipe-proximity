select *
from {{ ref('mart_sa1_accessibility_quadrants') }}
where
    (
        quadrant_code = 'Q4'
        and is_priority_quadrant is distinct from true
    )
    or
    (
        quadrant_code in ('Q1', 'Q2', 'Q3')
        and is_priority_quadrant is distinct from false
    )
    or
    (
        quadrant_code = 'UNCLASSIFIED'
        and is_priority_quadrant is not null
    )