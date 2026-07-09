# Volatility Bucket Edges

Volatility was smoothed with a trailing current/past rolling average before p33/p66 bucket edges were calculated.

## Global Fallback

- row_count: 9028391
- p33: 2.762814398229588
- p66: 4.909121856344038

## Partition Edges

| day_type   | session_bucket       |   row_count |     p33 |     p66 | used_global_fallback   |
|:-----------|:---------------------|------------:|--------:|--------:|:-----------------------|
| weekday    | ASIA_CORE_EARLY      |      534066 | 2.72062 | 4.2706  | False                  |
| weekday    | ASIA_DEEP_LOW        |      524736 | 3.44646 | 5.32845 | False                  |
| weekday    | ASIA_EUROPE_HANDOVER |      537849 | 2.62001 | 4.12269 | False                  |
| weekday    | ASIA_PRE_ACTIVE      |      534209 | 3.36685 | 5.0542  | False                  |
| weekday    | EUROPE_MIDDAY        |      536772 | 2.7521  | 4.30364 | False                  |
| weekday    | EUROPE_OPEN          |      536738 | 2.844   | 4.4352  | False                  |
| weekday    | US_AFTERHOURS_EARLY  |      527499 | 3.25607 | 5.12793 | False                  |
| weekday    | US_AFTERHOURS_LATE   |      527578 | 2.70386 | 4.32525 | False                  |
| weekday    | US_LATE_CORE         |      533014 | 4.35146 | 6.5396  | False                  |
| weekday    | US_OPEN_EARLY        |      525484 | 6.44202 | 9.53146 | False                  |
| weekday    | US_OPEN_LATE         |      528053 | 5.39216 | 7.8057  | False                  |
| weekday    | US_PREOPEN           |      522042 | 3.34385 | 5.49586 | False                  |
| weekend    | ASIA_CORE_EARLY      |      223706 | 1.52695 | 2.71103 | False                  |
| weekend    | ASIA_DEEP_LOW        |      217206 | 1.87471 | 3.18783 | False                  |
| weekend    | ASIA_EUROPE_HANDOVER |      223439 | 1.38494 | 2.57068 | False                  |
| weekend    | ASIA_PRE_ACTIVE      |      221886 | 1.78176 | 3.05111 | False                  |
| weekend    | EUROPE_MIDDAY        |      223486 | 1.36755 | 2.60901 | False                  |
| weekend    | EUROPE_OPEN          |      222618 | 1.42131 | 2.84996 | False                  |
| weekend    | US_AFTERHOURS_EARLY  |      221122 | 1.73316 | 3.23076 | False                  |
| weekend    | US_AFTERHOURS_LATE   |      220015 | 2.41721 | 4.77247 | False                  |
| weekend    | US_LATE_CORE         |      221393 | 1.83861 | 3.25387 | False                  |
| weekend    | US_OPEN_EARLY        |      222330 | 2.09126 | 3.74481 | False                  |
| weekend    | US_OPEN_LATE         |      222460 | 2.0905  | 3.58074 | False                  |
| weekend    | US_PREOPEN           |      220690 | 1.55454 | 3.08057 | False                  |
