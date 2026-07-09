# Volatility Bucket Edges

Volatility was smoothed with a trailing current/past rolling average before p33/p66 bucket edges were calculated.

## Global Fallback

- row_count: 7443464
- p33: 2.6720222041025816
- p66: 4.770468875743954

## Partition Edges

| day_type   | session_bucket     |   row_count |     p33 |     p66 | used_global_fallback   |
|:-----------|:-------------------|------------:|--------:|--------:|:-----------------------|
| weekday    | ASIA_ACTIVE        |      877630 | 2.71777 | 4.229   | False                  |
| weekday    | ASIA_LOW_LIQUIDITY |      654832 | 3.40887 | 5.25549 | False                  |
| weekday    | EUROPE_OPEN        |     1097611 | 2.70254 | 4.23653 | False                  |
| weekday    | US_AFTERHOURS      |      643963 | 2.69237 | 4.23964 | False                  |
| weekday    | US_LATE_SESSION    |      870619 | 4.25309 | 6.37203 | False                  |
| weekday    | US_OPEN_EXPANSION  |      653517 | 6.14329 | 9.04566 | False                  |
| weekday    | US_PREOPEN         |      433763 | 3.23911 | 5.40164 | False                  |
| weekend    | ASIA_ACTIVE        |      369773 | 1.46243 | 2.59559 | False                  |
| weekend    | ASIA_LOW_LIQUIDITY |      270859 | 1.79634 | 3.03013 | False                  |
| weekend    | EUROPE_OPEN        |      462540 | 1.31202 | 2.5507  | False                  |
| weekend    | US_AFTERHOURS      |      276768 | 2.05736 | 4.17443 | False                  |
| weekend    | US_LATE_SESSION    |      369326 | 1.76089 | 3.12549 | False                  |
| weekend    | US_OPEN_EXPANSION  |      277352 | 2.01271 | 3.57954 | False                  |
| weekend    | US_PREOPEN         |      184911 | 1.47548 | 2.93795 | False                  |
