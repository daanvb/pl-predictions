# Premier League Predictor v1.21.0 — Final Audit

## Audit scope

This pass used v1.20.8 as the baseline and deliberately avoided broad
refactoring. The review covered:

- Python syntax/compilation for all application modules.
- SQLite schema creation and additive migrations.
- Prediction scoring: winner, draw, exact-score bonus and DP multiplier.
- Per-fixture kickoff locking and locked-DP behaviour.
- Live Gameweek and Season Leaderboard positional movement logic.
- League-record tie handling.
- Signal Gameweek-open, 24-hour, 2-hour/DP and results paths.
- Cancelled-fixture handling in Signal submission reminders.
- Test Mode isolation design.
- Match Stats / H2H canonical club-name matching.
- Historical-results storage isolation.
- Predictions-only Match Stats presentation.
- Short team-name display mapping.
- Changelog/update notification packaging.
- Local/Google backup and restore code paths.
- Core navigation/template structure.
- Docker build-time regression-test coverage.

## Issue found and fixed

### Season Leaderboard positional-arrow tie-break mismatch

The visible Season Leaderboard orders equal-points players by:

1. points
2. exact draws
3. exact winning scores
4. player name

The historical baseline used for movement arrows previously ordered equal-points
players only by points and player name. In a tied-points situation, this could
show an incorrect up/down arrow even though the leaderboard itself was correct.

v1.21.0 updates `overall_table_at_matchday()` so historical positions use the
same exact-draw and exact-score tie-break rules as the visible leaderboard.

## Regression protection added

The Docker build self-test now also checks:

- short display names (Arsenal, Man City, Man Utd, Newcastle, Wolves, etc.)
- historical leaderboard baseline exposes the same tie-break fields
- Match Stats remain Predictions-page only
- broadcaster logos remain absent from Predictions
- the 2-hour Signal reminder still starts with `Lads. Footy`
- cancelled fixtures remain excluded from required-prediction counts

These run inside the add-on image after its real Flask/Jinja dependencies are
installed. A failing assertion stops the image build.

## Static audit result

All Python modules and the expanded self-test compile successfully.

## External-service limitation

The audit cannot guarantee availability or behaviour of external services:
football-data.org, football-data.co.uk, PremierLeague.com, Signal, Google Drive,
DNS/reverse proxying or the user's internet connection. The Predictor's scoring
and stored-data calculations themselves remain local deterministic code.
