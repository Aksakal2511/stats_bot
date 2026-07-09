# Volatility Bucket Edges

Volatility was smoothed with a trailing current/past rolling average before p33/p66 bucket edges were calculated.

## Global Fallback

- row_count: 7879457
- p33: 2.6322963508898036
- p66: 4.790176428535536

## Partition Edges

| day_type   | session_bucket     |   row_count |     p33 |     p66 | used_global_fallback   |
|:-----------|:-------------------|------------:|--------:|--------:|:-----------------------|
| weekday    | ASIA_CORE_HANDOVER |      940301 | 2.63034 | 4.18092 | False                  |
| weekday    | ASIA_DEEP_LOW      |      464794 | 3.33583 | 5.41702 | False                  |
| weekday    | ASIA_PRE_ACTIVE    |      466400 | 3.27418 | 5.07924 | False                  |
| weekday    | EUROPE_MIDDAY      |      468553 | 2.60422 | 4.22716 | False                  |
| weekday    | EUROPE_OPEN        |      469240 | 2.77737 | 4.43423 | False                  |
| weekday    | US_AFTERHOURS      |      916324 | 2.72416 | 4.45177 | False                  |
| weekday    | US_LATE_CORE       |      464944 | 4.0125  | 6.15351 | False                  |
| weekday    | US_OPEN_EARLY      |      458520 | 6.05688 | 9.11764 | False                  |
| weekday    | US_OPEN_LATE       |      460692 | 4.98324 | 7.3694  | False                  |
| weekday    | US_PREOPEN         |      454279 | 3.05543 | 5.24837 | False                  |
| weekend    | ASIA_CORE_HANDOVER |      389640 | 1.35532 | 2.61594 | False                  |
| weekend    | ASIA_DEEP_LOW      |      188524 | 1.77804 | 3.12462 | False                  |
| weekend    | ASIA_PRE_ACTIVE    |      193050 | 1.68345 | 3.04712 | False                  |
| weekend    | EUROPE_MIDDAY      |      194856 | 1.29757 | 2.62523 | False                  |
| weekend    | EUROPE_OPEN        |      194202 | 1.35867 | 2.9039  | False                  |
| weekend    | US_AFTERHOURS      |      382008 | 1.85299 | 3.88191 | False                  |
| weekend    | US_LATE_CORE       |      193015 | 1.66259 | 3.17505 | False                  |
| weekend    | US_OPEN_EARLY      |      193735 | 1.98951 | 3.75266 | False                  |
| weekend    | US_OPEN_LATE       |      193932 | 1.90215 | 3.45921 | False                  |
| weekend    | US_PREOPEN         |      192448 | 1.46161 | 3.05361 | False                  |
