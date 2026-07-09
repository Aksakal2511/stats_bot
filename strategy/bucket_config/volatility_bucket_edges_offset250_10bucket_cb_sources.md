# Volatility Bucket Edges

Volatility was smoothed with a trailing current/past rolling average before p33/p66 bucket edges were calculated.

## Global Fallback

- row_count: 7880193
- p33: 2.755863236170264
- p66: 4.971021373188825

## Partition Edges

| day_type   | session_bucket     |   row_count |     p33 |     p66 | used_global_fallback   |
|:-----------|:-------------------|------------:|--------:|--------:|:-----------------------|
| weekday    | ASIA_CORE_HANDOVER |      940301 | 2.63792 | 4.19264 | False                  |
| weekday    | ASIA_DEEP_LOW      |      459344 | 3.45406 | 5.48876 | False                  |
| weekday    | ASIA_PRE_ACTIVE    |      466400 | 3.36148 | 5.14431 | False                  |
| weekday    | EUROPE_MIDDAY      |      468553 | 2.75798 | 4.39984 | False                  |
| weekday    | EUROPE_OPEN        |      469240 | 2.82315 | 4.47127 | False                  |
| weekday    | US_AFTERHOURS      |      920649 | 2.95757 | 4.7322  | False                  |
| weekday    | US_LATE_CORE       |      464944 | 4.36056 | 6.56558 | False                  |
| weekday    | US_OPEN_EARLY      |      458520 | 6.48557 | 9.63511 | False                  |
| weekday    | US_OPEN_LATE       |      460692 | 5.40616 | 7.82737 | False                  |
| weekday    | US_PREOPEN         |      454279 | 3.27229 | 5.53964 | False                  |
| weekend    | ASIA_CORE_HANDOVER |      389640 | 1.42393 | 2.66733 | False                  |
| weekend    | ASIA_DEEP_LOW      |      188524 | 1.86331 | 3.23488 | False                  |
| weekend    | ASIA_PRE_ACTIVE    |      193050 | 1.76981 | 3.10914 | False                  |
| weekend    | EUROPE_MIDDAY      |      194856 | 1.34768 | 2.67763 | False                  |
| weekend    | EUROPE_OPEN        |      194142 | 1.40325 | 2.95322 | False                  |
| weekend    | US_AFTERHOURS      |      383989 | 2.00614 | 4.04515 | False                  |
| weekend    | US_LATE_CORE       |      192955 | 1.79426 | 3.31307 | False                  |
| weekend    | US_OPEN_EARLY      |      193735 | 2.08914 | 3.86908 | False                  |
| weekend    | US_OPEN_LATE       |      193932 | 2.03321 | 3.60623 | False                  |
| weekend    | US_PREOPEN         |      192448 | 1.54353 | 3.17419 | False                  |
