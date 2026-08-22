Premier League Predictor v1.0.0
Final Audited Release
=====================

This release is based on v1.20.8.

FINAL AUDIT
===========
The app was reviewed across:
- scoring + DP
- prediction / DP locking
- league tables + movement arrows
- tie-aware records
- Signal reminders/results
- Test Mode isolation
- Match Stats / H2H
- short team names
- database migrations
- backup / restore paths
- navigation / templates
- Docker regression tests

FIX
===
A real edge case was found in the main League movement arrows.

When players were tied on points, the historical arrow baseline did not use
the same exact-draw / exact-score tie-break rules as the visible leaderboard.

v1.21.0 makes the baseline and displayed table use the same ordering.

REGRESSION TESTS
================
The Docker build self-test now covers more of the newer functionality.
If one of those tested behaviours breaks, the addon image build fails.

See AUDIT_REPORT.md for details.

INSTALL
=======
1. Make/download a backup.
2. Replace the addon contents with v1.0.0.
3. DO NOT delete /data.
4. Rebuild/reinstall.
5. Confirm footer shows v1.0.0.


VERSIONING NOTE
===============
Predictor app version: 1.0.0
Home Assistant addon package version: 1.21.1

The addon package version must keep increasing because Home Assistant will not
install a numerically older version over an existing newer addon.


v1.0.1 DESKTOP LIVE GAMEWEEK FIX
================================
- Desktop only: wider centre score column
- Home score / dash / away score aligned independently
- Tabular score digits
- Mobile layout unchanged

Public app version: 1.0.1
Home Assistant package version: 1.21.2


v1.0.2 DASHBOARD LEAGUE POSITION
================================
Dashboard now shows:
- Season Total
- League Position
- Current Gameweek

League position uses the same ranking/tiebreak logic as the main League page.

Public app version: 1.0.2
Home Assistant package version: 1.21.3


v1.0.5 LIVE DATA RELIABILITY FIX
================================
The previous refresh loop could sleep for six hours even when a match kicked
off during that sleep. That could leave a fixture as Upcoming for the whole
match and delay score/results processing.

The worker now wakes automatically before the next stored kickoff and polls
every five minutes through the live window.

Important: football-data.org's Free plan advertises delayed scores, so the app
cannot make the provider deliver true real-time scores. It now makes the
refresh timing reliable and clearly shows when it is waiting for provider data.

Public app version: 1.0.5
Home Assistant package version: 1.21.7
