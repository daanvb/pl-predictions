# Changelog

All notable changes to Preddies are documented here.

## [1.2.5] - 2026-09-04

### Important
- Added the agreed formats and rules for both upcoming side competitions.

### New
- Published the agreed MCFG Cockfight Cup format, schedule, match points, tie-breaks and Gameweek 38 final rules.
- Published the Champions League prediction format and scoring rules for knockout-stage fixtures through to the final.

### Changes
- Added the reigning Premier League champion trophy beside the winner's name throughout ordinary player views, while keeping live graphs and statistics uncluttered.
- Improved the size and proportions of competition trophies in page headings and removed the generic gold trophy from Most League Wins.

### Fixes

#### UI
- Corrected the MCFG Cockfight Cup trophy proportions and made page-heading trophies easier to see.

## [1.2.4] - 2026-09-04

### Important
- Added a £30 entry-payment register. Payment status is visible to the league but can only be changed by the player assigned as Treasurer.

### New
- Added an admin-managed Treasurer role with payment-only permissions.
- Added dedicated Champions League and MCFG Cockfight Cup competition placeholders.
- Added separate Premier League, Champions League and MCFG Cockfight Cup sections to Past Winners.

### Changes
- Renamed League to Premier League and Side Events to Champions League, and added competition-specific menu icons.
- Published the £120 prize allocation: £50 Premier League winner, £30 runner-up, and £20 winners for both side competitions.

### Fixes

#### Database
- Added non-destructive player payment and Treasurer fields plus a future-ready side-competition winners table.

## [1.2.3] - 2026-09-02

### Important
- This visual update does not alter players, predictions, points or account preferences.

### New
- Added a left-hand slide-out dashboard menu for League, Side Events, statistics, history, rules, prizes, past winners and the changelog.
- Added prominent My Account, Tegridy and administrator icons to the dashboard header.

### Changes
- Reduced the dashboard stat-card height and arranged the season total, league position and Signal cards in one compact mobile row.

### Fixes

#### UI
- Enforced one consistent compact font size across every news ticker headline, including on iPhone where automatic text enlargement previously altered individual items.
- Improved the spacing and label fit inside the compact league-position card.
- Reduced and condensed the slide-out menu while retaining comfortable touch targets.
- Further narrowed the mobile menu and tightened its heading and navigation rows.
- Reserved a clear row above the mobile welcome card for the floating navigation and account controls.
- Kept the compact menu close control perfectly circular on iPhone.
- Prevented mobile minimum-height styling from stretching the menu close control into an oval.
- Lightened the BBC Sport logo panel in light mode while retaining its dark-mode contrast.
- Limited Live dashboard news headlines to items published within the previous 36 hours.
- Kept the live gameweek table and position graph directly below the news ticker.
- Spread the season-total and league-position content more evenly, enlarged their labels and reduced the season-points figure.

## [1.2.2] - 2026-09-01

### Important
- This update is additive and does not alter existing players, predictions, points or account preferences.

### New
- Added an automatically scrolling BBC Sport Premier League news ticker beneath the Signal button on the live dashboard, with publication times.

### Changes
- Added a My Account preference allowing each player to hide the news ticker.

### Fixes

#### UI
- Standardised all ticker headlines to the same compact font size.

## [1.2.1] - 2026-09-01

### Important
- Existing predictions are preserved and automatically registered as the starting point of the new integrity ledger during the update.
- The migration is additive: it does not recalculate scores, change points or alter submitted predictions.

### New
- Added a shared Tegridy page where every player can confirm that saved predictions agree with the protected record.
- Added concealed score commitments so prediction values remain hidden on Tegridy until the relevant fixture kicks off, including when an integrity problem is being investigated.
- Added an immutable, hash-chained prediction ledger with a visible health check and retained revision history.

### Changes
- Restricted filesystem access to the live database, application secret, restore uploads and local backups.
- Enabled stronger SQLite deletion, temporary-storage and trusted-schema protections.
- Simplified Tegridy in plain English and kept detailed prediction records hidden while all integrity checks pass. Audit details are shown to the group only if the chain is broken or the saved predictions no longer match it, with failures highlighted clearly in red.

### Fixes
- Database integrity checks now detect prediction values that no longer agree with the latest recorded ledger entry.
- Database audit rows are protected against accidental updates or deletion.

## [1.2.0] - 2026-09-01

### New
- Added a new Side Events area ready for future prediction competitions.
- Added an option to remember an email address or username on the sign-in device.
- Added a remembered light and dark display mode, controlled by an accessible icon in the top-right corner.

### Changes
- Refreshed the sign-in screen with the Preddies logo and a cleaner layout.
- Reduced the vertical height of the season and live position charts.
- Aligned the season position chart more closely with its legend and adapted its labels and grid lines to both display modes.
- Refined dark mode with navy scorecards and team-stat panels, clearer controls and a new line-art theme icon.
- Reorganised release notes into Important, New, Changes and categorised Fixes sections.
- Simplified the Signal gameweek-open announcement with the gameweek, first kick-off and Preddies link.

### Fixes
- Added broadcaster logos to Admin Test fixture cards using the same layout as the live scorecards.
- Switched TNT Sports to a transparent white wordmark in dark mode for better readability.
- Added a red outline to the dark-mode Log out button.
- Removed the duplicate plain FT line from completed gameweek result cards while retaining the FT badge.

## [1.1.19] - 2026-09-01

### Changed
- Internal maintenance release.

## [1.1.18] - 2026-09-01

### Fixed
- Shifted the live position graph and its numbered scale further left for cleaner alignment.
- Kept every fixture label centred beneath its corresponding graph point, including the final label.

## [1.1.17] - 2026-08-31

### Changed
- Left-aligned the live position graph and added a visible league-position scale.
- The dashboard Make Predictions action now disappears when the gameweek's final fixture kicks off.
- Removed the redundant Live GW link from the prediction entry page.

## [1.1.16] - 2026-08-31

### Fixed
- Split compact live-graph markers into separate score and team-code lines, with balanced spacing so fixture labels and position lines remain readable.

### Changed
- League Stats now labels double-point exact predictions as correct scores and reports completed gameweeks instead of completed fixtures.
- Signal gameweek results now show medals for the top three overall league positions.
- Tied points within live scorecards are now ordered by current overall league position.

## [1.1.15] - 2026-08-31

### Changed
- Compacted live graph changes to half their previous horizontal spacing.
- Centred the Make Predictions action on the gameweek card.

### Fixed
- Kept the match status and TV channel row directly above predictions and points on every score card.

## [1.1.14] - 2026-08-31

### Changed
- Restored the separate centred match status and right-aligned TV logo beneath each fixture's score and match details.
- Increased mobile graph spacing and reduced event-label text so fixture score labels remain readable.

## [1.1.13] - 2026-08-31

### Changed
- Moved broadcaster and match status information into a clear row immediately above each card's predictions and points.
- The live table and position graph now appear only after the gameweek's first fixture kicks off, and the redundant Live Table button has been removed.
- Player names above the live graph can now be tapped to highlight an individual line.

### Fixed
- Ensured the live graph continues to render this gameweek's saved position data while preserving valid longer-lived changes.
- When live snapshots are missing, the graph now reconstructs settled position changes by replaying the gameweek's completed results in order.

## [1.1.12] - 2026-08-31

### Added
- Added every player's revealed prediction and provisional points directly to each live dashboard score card; future fixtures remain locked until kick-off.

### Changed
- Moved the live gameweek league table and position graph above the score cards.

### Fixed
- Restored legitimate position changes from this gameweek by limiting bounce suppression to brief provider reversals rather than removing longer match-driven changes.

## [1.1.11] - 2026-08-31

### Added
- Added the live gameweek table and position chart directly to the main dashboard, while retaining the existing Live GW URL for compatibility.

### Fixed
- The position chart now remains visible when filtering leaves one settled state.
- Current-season match stats now recognise date-valid results even if an imported row carried an incorrect season tag.
- Corrected match stats to show each team's current-season Premier League record across all league matches; home and away now identify the fixture sides rather than filtering the record by venue.

## [1.1.10] - 2026-08-31

### Fixed
- Replaced unhelpful live-chart labels with football-event labels for new data and a clear position-change label for legacy records.
- Collapsed repeated recalculations for the same score event so provider noise does not create long flat or bouncing graph runs.

## [1.1.9] - 2026-08-31

### Improved
- Exact-score predictions now highlight the complete player result row.
- Live position points now identify the fixture and score that caused the table movement.
- The position graph hides legacy points where nobody changed position and uses wider transient filtering to reduce provider-related bouncing.
- Home/away venue records now include scored matches from earlier gameweeks even when a provider left their status stale.
- Home/away records now receive a separate league-only football-data.org refresh when a new gameweek opens.

## [1.1.8] - 2026-08-28

### Improved
- Refined the mobile Predictions layout with centred score inputs, consistent spacing and club crests kept directly beside their team names.
- Standardised season league-table headings as Correct Draws, Correct Scores and Correct Winners, with a clearer tie-break explanation.

## [1.1.7] - 2026-08-27

### Fixed
- Returned prediction-screen crests to the outside of their respective team names.
- Prevented team containers from stretching, keeping every crest adjacent to its name with consistent score-box spacing and left-aligned wrapping.

## [1.1.6] - 2026-08-27

### Improved
- Grouped each club crest beside the score-facing edge of its team name on the mobile Predictions screen.
- Added consistent spacing between both team clusters and their score inputs while retaining left-aligned wrapped names.

## [1.1.5] - 2026-08-27

### Improved
- Left-aligned wrapped team-name lines consistently on the mobile Predictions screen.
- Vertically aligned club crests with their team names and score boxes.
- Restored Save Predictions as a smaller floating disk button positioned on the right, with a compact mobile label.

## [1.1.4] - 2026-08-27

### Improved
- Kept wrapped club names and crests neatly contained and aligned on the mobile Predictions screen.
- Returned the Save Predictions action to the end of the fixture list so it no longer overlaps cards.
- Simplified the Predictions screen by removing the redundant Double Points explanation card.
- Clarified that live Gameweek match data is supplied by SportScore and football-data.org.

## [1.1.3] - 2026-08-27

### Added
- Split personal performance into Your Stats and added a dedicated Overall League Stats page.
- Added each player's best Gameweek to Your Stats.

### Improved
- Renamed the app to Preddies across the interface, Home Assistant listing and notifications.
- Simplified headings and tightened layouts across Stats, Past Winners, Gameweek History, My Account and the season leaderboard.
- Clarified the standard and Double Points scoring examples on the Rules page.

### Fixed
- Corrected Double Points scoring so the complete award, including an exact-score bonus, is doubled: 10 points for an exact winning score and 12 points for an exact draw.
- Finished predictions are recalculated using the corrected scoring rules so affected earlier Gameweeks update automatically.

## [1.1.2] - 2026-08-27

### Improved
- Tightened typography, cards and spacing across Stats, Rules, Past Winners and Gameweek History.
- Made the Live Gameweek table more compact while retaining clear positions, movement and points.
- Reduced empty space beneath fixture status and broadcaster details.
- Shifted and aligned the season leaderboard position column for easier scanning.

## [1.1.1] - 2026-08-27

### Improved
- Refined mobile fixture cards so wrapped team names, crests, kickoff details, status badges and broadcaster logos remain consistently aligned.
- Kept the main live dashboard presentation in step with its live-data feed, including added time, scorers, penalties and red cards.
- Added a single combined source note beneath the fixture list when SportScore and football-data.org contribute data.
- Simplified the dashboard navigation and summary area, moved the public Signal group link beside the season summary and clarified the current-round wording.
- Tightened the season leaderboard on mobile, centred its position column and clarified the correct-draw, correct-score and correct-winner totals.
- Made the season position chart interactive and shortened long chart labels for easier reading.
- Removed unavailable season links and redundant prediction counters from Stats and Admin.

### Fixed
- Standardised league ordering everywhere as points, correct draws, correct scores, correct winners, then player name.
- Reduced unnecessary database writes when recalculating already-current prediction points.

## [1.1.0] - 2026-08-26

### What's new since Gameweek 1
- The live dashboard now follows scores, match clocks, added time, scorers, penalties and red cards as games unfold.
- The live gameweek table recalculates provisional points and league positions throughout each match.
- Predictions remain private until each individual fixture kicks off, then become visible to the league.
- Prediction cards now include club badges, clearer score entry and expanded form and head-to-head information.
- Player stats, league records and gameweek history provide a clearer view of performance across the season.
- Completed gameweeks remain visible overnight, with the next round and its Signal announcement opening at 09:00 the following day.

### Added
- Added a live gameweek position chart that records how provisional league positions change as scores and points move.
- Position changes remain available from the completed gameweek's Results / Table history view.

### Improved
- Live fixture cards now use a red border matching the existing LIVE indicator.
- Gameweek progress text now clearly distinguishes completed matches from matches currently in progress.
- Refined live dashboard fixture spacing and team sizing on mobile while retaining the kickoff-above-status layout.
- Player names in the season position chart can now be selected to highlight an individual line, and longer display names use their compact form.
- Centred the dashboard season total, league position and current gameweek summary cards, including the final card on mobile.
- Standardised club badge columns across fixture cards so team logos remain aligned when names wrap.
- Reworked the season leaderboard into compact mobile rows with points immediately visible and no horizontal scrolling.
- Brought club badges directly beside team names with consistent spacing on mobile and desktop fixture cards.
- Aligned broadcaster logos with the fixture status row and aligned the dashboard prediction and Live GW actions with the fixture-card edges.
- Tightened crest and team-name alignment, added clearer kickoff/status spacing and moved diagnostic source labels below the status.
- Simplified penalty goals to an inline `(Pen)` marker beside the scoring minute.
- Corrected wrapped team-name alignment so each club crest remains visually attached to its name.
- Refined two-line mobile team alignment and tightened the matchup around the central `v` or score.
- Realigned the mobile season table, clarified its ranking tie-breaks and expanded its correct-result labels.

## [1.0.29] - 2026-08-26

### Fixed
- Live match cards now reproduce SportScore's added-time clock, including values such as 45+3 and 90+2.

## [1.0.28] - 2026-08-26

### Fixed
- Live match clocks now retain SportScore added time when the base minute and added-time value arrive in separate fields.

### Changed
- Moved the account data notice and prediction reveal rule directly below their Dashboard buttons for better visibility.

## [1.0.27] - 2026-08-26

### Changed
- Simplified the Your Stats cards by removing the explanatory point-value text beneath each total.

## [1.0.26] - 2026-08-26

### Fixed
- Live match clocks now preserve added time such as 45+3 and 90+2, and show HT correctly at half-time.

### Improved
- The completed gameweek now remains on the dashboard overnight; the next gameweek and its Signal announcement open together at 09:00 the following day.
- Improved database responsiveness when live updates and users access the app at the same time.
- Strengthened PIN security and added protection against repeated unsuccessful login attempts.
- Made Home Assistant builds more reliable by locking release dependencies to tested versions.

## [1.0.25] - 2026-08-25

### Fixed
- Added a verified fallback for Arsenal's badge when SportScore omits the team logo.

## [1.0.24] - 2026-08-25

### Fixed
- Championship head-to-head results now match short source names such as Hull and Coventry to their current club names.
- Improved score spacing on live fixture cards and centred mobile kickoff dates and times.

## [1.0.23] - 2026-08-25

### Added
- Live fixture cards can now show penalties and red-card incidents beneath the relevant team.
- Head-to-head history now includes previous Championship meetings between the same clubs.

### Fixed
- Club badges are now delivered reliably to mobile devices.
- Improved spacing and alignment for live scores, goalscorers and match incidents.

## [1.0.22] - 2026-08-25

### Changed
- Gameweek History now runs chronologically from GW1 through GW38.
- Live match cards recognise added-time clocks such as 45+4 and 90+2, align goalscorers beneath their teams, and no longer repeat the live status below the score.

## [1.0.21] - 2026-08-25

### Changed
- League Records cards now use the first part of multi-word usernames to prevent wrapped names looking like separate tied players.

## [1.0.20] - 2026-08-25

### Fixed
- Added a verified SportScore fallback for Chelsea's badge when its team API response omits the logo.

## [1.0.19] - 2026-08-25

### Added
- Added automatic, low-rate SportScore badge discovery so missing team logos populate across fixtures and dashboards independently of live scoring.

### Changed
- Simplified Make Predictions cards by removing the duplicated fixture heading and using `v` between the score inputs.

## [1.0.18] - 2026-08-25

### Added
- Added SportScore team names and club badges, plus the Predictor logo on the dashboard.

### Changed
- Live gameweek players now follow the current league order.
- Simplified gameweek history and delayed the existing Signal gameweek-open message by 15 minutes.
- Refreshed the interface typography with SportScore's Inter font styling.

## [1.0.17] - 2026-08-24

### Fixed
- SportScore direct match lookups now try both team orders because its canonical match URLs are not consistently ordered.
- A missing scorer archive returning HTTP 404 no longer aborts the whole gameweek refresh or blocks the active match.
- Empty scorer data from the slower Football-Data fallback no longer erases goalscorers already obtained from SportScore.

### Changed
- Home Assistant package version updated to `1.21.20`.

## [1.0.16] - 2026-08-24

### Fixed
- Live updates now query each active Premier League fixture directly instead of relying on SportScore's global latest-50 feed, which could omit evening matches among worldwide fixtures.
- Current scores, match minutes and goalscorers can now reach the dashboard on the intended one-minute refresh cycle.

### Changed
- Home Assistant package version updated to `1.21.19`.

## [1.0.15] - 2026-08-24

### Changed
- Renamed the Historical Winners dashboard tab and page to `Past Winners` and moved the tab directly after Prize Structure.
- Home Assistant package version updated to `1.21.18`.

## [1.0.14] - 2026-08-24

### Added
- Added the recorded champions from 2018/19 through 2023/24: Strat (three titles), TROPiC (two), and Percei (one).
- Historical Winners now calculates the all-time title record across all eight recorded seasons.
- Added a visible Logout button and a safe “Remember my email” login option; browser credential managers can save the PIN when the user chooses.

### Changed
- Home Assistant package version updated to `1.21.17`.

## [1.0.13] - 2026-08-24

### Added
- Added SportScore-first one-minute live updates, persistent goalscorers and an Admin retry for current-gameweek scorer gaps; Football-Data remains the fixture/results fallback.
- Added Historical Winners with Fontz (2024/25), TROPiC (2025/26), the all-time most-league-wins record and automatic full season snapshots from 2026/27 onward.
- Added a crowned-football Predictor logo for Home Assistant, browser favicons and app branding.

### Improved
- Live Gameweek player predictions are ordered by the points earned in each match.
- Season League statistics now use separate exact-draw, exact-win and other-correct columns, plus new DP exact-score and league-record statistics.
- Signal manual messages no longer cause automatic duplicates, and Signal tables now use the same exact-score tie-breaks as the main league.

### Removed
- Retired Test Mode and moved Changelog to the final dashboard navigation position.
- Home Assistant package version updated to `1.21.16`.

## [1.0.12] - 2026-08-24

### Changed
- Players now log in with an email address and their existing PIN while their editable username remains the public display name.
- Existing accounts are migrated safely: the old login name continues to work until an email is added in My Account or by an administrator.
- New registrations and admin-created players require a unique email address.
- Home Assistant package version updated to `1.21.15`.

## [1.0.11] - 2026-08-24

### Changed
- Login names are now separate from editable display names. Existing players keep their current name as their permanent login name during migration.
- Players and admins can change a display name without changing that player's login credentials, predictions, points or history.
- The Account page clearly shows the fixed login name, and the login screen labels it explicitly.
- Home Assistant package version updated to `1.21.14`.

## [1.0.10] - 2026-08-24

### Added
- A responsive continuous line chart beneath the Season Leaderboard showing every player's league position after each completed gameweek.
- Each player has a consistent colour, labelled legend and position markers; first place is shown at the top of the chart.
- Home Assistant package version updated to `1.21.13`.

## [1.0.9] - 2026-08-24

### Changed
- Replaced the current-season-restricted API-Football backup with SportScore, which requires no token or account.
- The Dashboard again shows only the automatically selected current gameweek; manual previous/next gameweek controls were removed.
- SportScore supplements live and just-finished current-gameweek fixtures with scores, minutes and persistent goal events.
- Home Assistant package version updated to `1.21.12`.

## [1.0.8] - 2026-08-24

### Fixed
- The Admin overview now provides visible links to Players, Fixtures, API Settings and Backups, making the optional API-Football token form reachable without manually editing the URL.
- Home Assistant package version updated to `1.21.11`.

## [1.0.7] - 2026-08-24

### Added
- Previous and next gameweek controls on the main Dashboard keep stored final scores and goalscorers browsable throughout the season.
- Finished fixtures with missing goal events are backfilled in small, rate-limit-safe batches.
- Optional API-Football backup-feed support for live Premier League scores, match status, minutes and goalscorers.
- The main football-data.org feed remains authoritative for the fixture schedule; secondary data is only applied to an exact home/away team match.

### Improved
- Live refreshes now run every three minutes instead of every five minutes.
- API-Football is only queried during live match windows to protect its 100-request free daily allowance.
- Public Predictor version updated to `1.0.7`; Home Assistant package version updated to `1.21.10`.

## [1.0.6] - 2026-08-24

### Added
- The Dashboard now shows live scores while Premier League fixtures are in progress.
- Current goalscorers and goal minutes appear beneath the correct home or away team when the football-data provider supplies goal events.
- Penalties, own goals and stoppage-time minutes are labelled in the scorer list.
- The Dashboard refreshes itself every minute while a fixture is live.

### Improved
- Live scorer details are retained if a later API response temporarily omits its goal-event payload.
- The app requests match details for live fixtures when the competition feed does not include goal events.
- Existing prediction locking and finished-match points recalculation are unchanged.

### Packaging
- Public Predictor version updated to `1.0.6`.
- Home Assistant app package version updated to `1.21.9`.














































## [1.0.5] - 2026-08-21

### Fixed
- Fixed the live-data refresh scheduler being able to sleep through a Premier League kickoff.
- The app now calculates its next wake-up from the next stored fixture kickoff instead of blindly sleeping for six hours.
- API polling switches to the existing 5-minute interval from 20 minutes before kickoff until three hours after kickoff.
- Finished results continue to trigger the existing local points recalculation immediately after the API refresh.

### Improved
- A fixture whose stored kickoff has passed can no longer continue to display `Upcoming` indefinitely while the provider is late updating its status.
- During a provider delay it displays `LIVE · awaiting score`, then `Awaiting result` after the live window, without fabricating a score or writing an inferred result to the database.
- Admin Football Data now records the most recent API refresh error for troubleshooting.
- Added refresh-worker logging showing when the next API check is scheduled.

### Packaging
- Public Predictor version updated to `1.0.5`.
- Home Assistant add-on package version updated to `1.21.7`.

## [1.0.4] - 2026-08-21

### Added
- Added a new `Prize Structure` tab with a placeholder page ready for prize details.

### Packaging
- Public Predictor version updated to `1.0.4`.
- Home Assistant add-on package version updated to `1.21.5`.

## [1.0.2] - 2026-08-21

### Added
- Added current `League Position` to the main Dashboard alongside Season Total and Current Gameweek.
- Top three positions display medal icons.
- Dashboard league position uses the same tie-break rules as the main Season Leaderboard: points, exact draws, exact winning scores, then player name.

### Packaging
- Public Predictor version updated to `1.0.2`.
- Home Assistant add-on package version updated to `1.21.3`.

## [1.0.1] - 2026-08-21

### Fixed
- Fixed Live Gameweek score/dash overlap on desktop.
- Desktop Live Gameweek now reserves a dedicated 84px centre score column between the team names.
- Home score, dash and away score are aligned in separate fixed cells with tabular numerals.
- Mobile Live Gameweek layout is intentionally unchanged.

### Packaging
- Public Predictor version updated to `1.0.1`.
- Home Assistant add-on package version updated to `1.21.2`.

## [1.0.0] - 2026-08-21

### Packaging
- Public Predictor version remains `1.0.0`.
- Home Assistant add-on package version is `1.21.1` so Hassio can upgrade from the previous `1.21.0` package.


### Release
- Renumbered the final audited build as the official `1.0.0` release.
- Functionality is unchanged from the audited v1.21.0 build.

## [1.21.0] - 2026-08-21

### Audit
- Completed a final full audit using v1.20.8 as the baseline.
- Reviewed scoring, DP, kickoff locking, live/main tables, records, Signal, Test Mode, Match Stats/H2H, display names, database migrations, backups, navigation and build-time regression protection.
- Added an updated `AUDIT_REPORT.md` to the release.

### Fixed
- Fixed a Season Leaderboard positional-arrow edge case for players tied on points.
- Historical leaderboard positions now use the same tie-break order as the visible table: points, exact draws, exact winning scores, then player name.

### Reliability
- Expanded the Docker build-time regression suite for short team names, positional baseline fields, Predictions-only Match Stats, no TV logos on Predictions, 2-hour Signal wording and cancelled-fixture reminder handling.
- App and Home Assistant version updated to `1.21.0`.

## [1.20.8] - 2026-08-21

### Changed
- Changed the display name `Newcastle Utd` back to `Newcastle`.
- `Man Utd` and `Sheffield Utd` remain shortened.
- App and Home Assistant version updated to `1.20.8`.

## [1.20.7] - 2026-08-21

### Changed
- Shortened `United` to `Utd` in display names, including `Man Utd`, `Newcastle Utd` and `Sheffield Utd`.
- This remains display-only; stored/API team names are unchanged.
- App and Home Assistant version updated to `1.20.7`.

## [1.20.6] - 2026-08-21

### Changed
- Team names across the app now use concise display names such as `Arsenal`, `Brentford`, `Wolves`, `Man City`, `Man United`, `Spurs`, `Newcastle`, `Nott'm Forest` and `West Ham`.
- Short names are display-only. Original API/database team names are retained internally so fixture imports, scoring, predictions and Match Stats matching are unaffected.
- Added sensible short-name fallbacks for test and historical clubs including `Coventry`, `QPR`, `West Brom`, `Norwich` and `Watford`.
- App and Home Assistant version updated to `1.20.6`.

## [1.20.5] - 2026-08-21

### Fixed
- Fixed Home/Away records and Recent Form failing when stored results used a different club-name variant.
- Home record, away record, recent form and H2H now all use the same canonical team-name matching.
- This fixes cases such as `Arsenal` vs `Arsenal FC`, `Man City` vs `Manchester City FC`, and similar source-name differences.

### Changed
- Corrected Admin Match Stats History wording to say the previous five Premier League seasons are attempted.
- App and Home Assistant version updated to `1.20.5`.

### Privacy / Processing
- Match Stats remain deterministic local Python/SQL calculations.
- No AI, LLM or OpenAI API is used.

## [1.20.4] - 2026-08-21

### Fixed
- Fixed Head-to-Head history remaining empty after historical refreshes.
- Historical seasons are now imported independently, so one inaccessible season cannot abort the entire refresh.
- Added deterministic football-data.co.uk Premier League result fallback when football-data.org does not expose an older season.
- Added club-name normalisation across data sources so names such as `Man City` and `Manchester City FC`, or `Wolves` and `Wolverhampton Wanderers FC`, correctly match for H2H.
- Historical refresh now commits every successful season independently.
- Admin now shows the historical sources used and any season-specific failures.

### Changed
- Match Stats now appear only on the Predictions page.
- Removed Match Stats from Dashboard and Live Gameweek.
- Removed Sky Sports / TNT Sports logos from the Predictions page.
- Historical refresh now attempts the previous five Premier League seasons.
- App and Home Assistant version updated to `1.20.4`.

### Privacy / Processing
- Match Stats and H2H remain deterministic local Python/SQL processing.
- No AI, LLM or OpenAI API is used.

## [1.20.3] - 2026-08-21

### Changed
- Replaced thick fixture separator lines with individual fixture cards.
- Each match now has its own self-contained visual box containing teams, date/time, broadcaster logo, status and Match Stats.
- Removed the heavy divider between fixtures.
- Added a subtle border, light shadow and consistent gap between matches.
- Match Stats panels remain visually subordinate inside the parent fixture card to avoid a box-within-box cluttered look.
- Mobile fixture cards use slightly tighter spacing and smaller corner radius for cleaner scrolling.
- App and Home Assistant version updated to `1.20.3`.

## [1.20.2] - 2026-08-21

### Changed
- Simplified fixture separation to a single thick grey line between complete fixture + Match Stats sets.
- Removed the extra lower border that caused the double-line appearance.
- App and Home Assistant version updated to `1.20.2`.

## [1.20.1] - 2026-08-21

### Changed
- Added clearer visual separation between each fixture and its Match Stats block.
- Increased spacing between fixture sets.
- Added a stronger divider/accent beneath fixture groups on mobile.
- Fixture cards on Predictions and Live Gameweek now use a more obvious lower border so neighbouring matches do not visually run together.
- App and Home Assistant version updated to `1.20.1`.

## [1.20.0] - 2026-08-21

### Added
- Added expandable `📊 Match Stats` panels to fixtures on the Dashboard, Predictions and Live Gameweek pages.
- Added current-season home record for the home team: W/D/L, goals for, goals against and points per game.
- Added current-season away record for the away team: W/D/L, goals for, goals against and points per game.
- Added last-5 recent form for both teams using locally stored completed results.
- Added head-to-head summary and up to five previous stored meetings.
- Added a separate `historical_fixtures` database table so historical results can power stats without affecting live fixtures or predictions.
- Added automatic one-time attempt to import the previous three Premier League seasons using the existing football-data API token.
- Added Admin → `Refresh Historical Results` for manually refreshing the local historical archive.

### Privacy / Processing
- Match Stats use deterministic Python/SQL calculations only.
- No AI, OpenAI API, LLM or AI-generated analysis is used.
- Once fixture/result data has been downloaded, all Match Stats calculations are performed locally.

### Reliability
- Historical data is isolated from live Predictor fixtures and cannot affect prediction foreign keys, locking or scoring.
- Duplicate match IDs are de-duplicated when current and historical data are combined.
- If the football-data API plan does not allow older seasons, current-season home/away records and recent form continue to work.
- A failed historical import does not stop the Predictor from starting or refreshing current fixtures.

### Changed
- App and Home Assistant version updated to `1.20.0`.

## [1.19.1] - 2026-08-21

### Changed
- League Records are now fully tie-aware.
- Current Leader shows every player sharing the highest points total.
- Most Exact Draws shows every player tied for the record.
- Most Exact Winning Scores shows every player tied for the record.
- Best Single Gameweek shows every tied record holder and the Gameweek in which each achieved the score.
- Tied record cards are clearly labelled `tied` / `tied record`.
- App and Home Assistant version updated to `1.19.1`.

## [1.19.0] - 2026-08-21

### Added
- Added live positional movement indicators to the Live Gameweek table.
- Green `▲` shows a player moving up; red `▼` shows a player moving down.
- Movement of more than one place displays the number of places moved.
- Live Gameweek movement compares the current provisional table with the same Gameweek using only fully finished fixtures, so the arrows reflect movement caused by matches currently in play.
- Added the same positional movement indicators to the main Season Leaderboard.
- Season Leaderboard movement uses the standings at the start of the current Gameweek as its baseline; between rounds it compares with the previous completed Gameweek.

### Changed
- Rebuilt navigation tabs as a centred responsive grid.
- Mobile navigation now uses equal-height, centred tabs with consistent spacing.
- Normal phone widths use a clean two-column layout; very narrow screens fall back to one column.
- Desktop navigation also receives consistent centring and alignment.
- App and Home Assistant version updated to `1.19.0`.

## [1.18.1] - 2026-08-21

### Changed
- Rebuilt fixture rows around a proper responsive grid instead of relying on inline flex wrapping.
- Home team, centre score/`v`, and away team now align consistently across every fixture.
- Date/time/status information now has its own dedicated row/column rather than competing with team names.
- TV broadcaster logos now occupy a fixed, centred slot so Sky/TNT logos line up from fixture to fixture.
- Mobile fixture status badges now align consistently beneath each match.
- Applied the same structured fixture header to Dashboard, Live Gameweek and Predictions pages.
- App and Home Assistant version updated to `1.18.1`.

## [1.18.0] - 2026-08-21

### Changed
- Added a dedicated mobile layout for screens up to 640px wide.
- Tightened fixture spacing and typography so match lists are easier to scan on phones.
- Improved home/away team alignment around the score/prediction area.
- Reduced broadcaster logo size further on mobile and centred logos beneath fixture information.
- Made prediction score inputs more compact and touch-friendly.
- Reduced card, navigation and table spacing on small screens while leaving the desktop layout unchanged.
- App and Home Assistant version updated to `1.18.0`.

## [1.17.1] - 2026-08-21

### Changed
- Made the Sky Sports and TNT Sports fixture logos slightly smaller.
- Improved centring and alignment of broadcaster logos within fixture rows.
- App and Home Assistant version updated to `1.17.1`.

## [1.17.0] - 2026-08-21

### Added
- Added UK TV broadcaster logos to Premier League fixtures.
- Sky Sports and TNT Sports logos are shown on televised fixtures; non-televised fixtures remain uncluttered.
- Broadcaster data is refreshed from the official Premier League 2026/27 fixture listing when fixtures refresh.
- Added a conservative fallback for standard UK weekend broadcast slots if the Premier League listing is temporarily unavailable.
- Added `Refresh UK TV listings` to Admin > Fixtures.
- Added a persistent `broadcaster` field to fixtures without affecting existing prediction data.

### Changed
- BT Sport is represented by its current name, TNT Sports.
- App and Home Assistant version updated to `1.17.0`.

## [1.16.3] - 2026-08-21

### Changed
- Moved the Signal group shortcut to the first position in the main Dashboard navigation.
- Renamed the Signal group shortcut to `Get Your Pre-Dicks In`.
- Changed the 2-hour Signal reminder heading to `Lads. Footy`.
- The 2-hour reminder still lists incomplete predictions and missing DP selections underneath the new heading.
- App and Home Assistant version updated to `1.16.3`.

## [1.16.2] - 2026-08-21

### Changed
- Gameweek Open Signal notification wording changed to `Get Your Pre-Dicks In`.
- App and Home Assistant version updated to `1.16.2`.

## [1.16.1] - 2026-08-21

### Changed
- Replaced the generic Signal chat emoji on the Dashboard shortcut with Signal's official logo.
- Gameweek Open Signal notification now says `Get your predicks in`.
- App and Home Assistant version updated to `1.16.1`.

## [1.16.0] - 2026-08-21

### Audit
- Added a build-time regression self-test that runs inside the real add-on image after Flask and production dependencies are installed.
- Self-test covers scoring, DP, database migrations, core page rendering, kickoff logic, Signal DP reminders, completed-Gameweek detection and the in-app changelog parser.
- Added `AUDIT_REPORT.md` to the release package.

### Fixed
- Signal Results could be missed when a Gameweek finished before the next Gameweek had been imported.
- Manual Signal Results now selects the latest fully completed Gameweek.
- Signal Gameweek results now retain registered players with zero points/no predictions.
- Cancelled fixtures no longer count as required predictions in Signal completion reminders.

### Reliability
- Every future Docker build now compiles the Python modules and executes the regression self-test; a failed test stops the image build.

### Changed
- App and Home Assistant version updated to `1.16.0`.

## [1.15.2] - 2026-08-21

### Added
- Added a `Signal Group` shortcut to the main Dashboard.
- The shortcut opens the Predictor's Signal group link in a new tab/window so mobile devices can hand it off to Signal.

### Changed
- App and Home Assistant version updated to `1.15.2`.

## [1.15.1] - 2026-08-21

### Added
- New per-player app update notification on the main Dashboard.
- Dashboard shows an `APP UPDATED` banner when the installed Predictor version has not yet been viewed by that player.
- The Changelog navigation link displays a `NEW` badge while release notes are unread.
- Opening the Changelog marks the current app version as seen for that player's account.

### Reliability
- Changelog read-state is stored persistently per player, so the update notification stays dismissed after logout, restart or addon rebuild.
- Each player sees and clears their own update notification independently.

### Changed
- App and Home Assistant version updated to `1.15.1`.

## [1.15.0] - 2026-08-21

### Changed
- Streamlined navigation throughout the app.
- Main Dashboard is now the primary navigation hub.
- Changelog, Rules, Stats, League and History pages now simply return to Dashboard instead of linking to unrelated pages.
- Prediction and Live Gameweek pages now use contextual navigation between each other and Dashboard.
- Test Mode links to Dashboard, plus Admin only when viewed by an administrator.
- Admin child pages now consistently show `Admin` and `Dashboard` navigation.
- Removed unnecessary cross-links that had accumulated as features were added.
- App and Home Assistant version updated to `1.15.0`.

## [1.14.5] - 2026-08-21

### Fixed
- Fixed `500 Internal Server Error` on the Changelog page caused by Jinja resolving `section.items` as Python's built-in dictionary method.
- Changelog template now uses explicit dictionary key access such as `section["items"]`.
- Other changelog dictionary fields were also changed to explicit key access to avoid similar name collisions.

### Changed
- App and Home Assistant version updated to `1.14.5`.

## [1.14.4] - 2026-08-21

### Fixed
- Fixed `500 Internal Server Error` when opening the in-app Changelog.
- Added the missing Python `re` import required by the changelog parser.
- Added package-time validation of the changelog parser against the bundled `CHANGELOG.md`.

### Changed
- App and Home Assistant version updated to `1.14.4`.

## [1.14.3] - 2026-08-21

### Fixed
- Fixed the in-app Changelog page showing `CHANGELOG.md could not be read from the app package`.
- `CHANGELOG.md` is now copied into the running app container at `/app/CHANGELOG.md`.
- The in-app changelog reader now checks the container path first.

### Changed
- App and Home Assistant version updated to `1.14.3`.

## [1.14.2] - 2026-08-21

### Changed
- Changelog is now accessible from the main Dashboard only.
- Removed the Changelog link from the login page.
- Removed the duplicate Changelog shortcut from Admin.
- Changelog again requires a logged-in player session.
- App and Home Assistant version updated to `1.14.2`.

## [1.14.1] - 2026-08-21

### Changed
- The in-app Changelog is now public and can be viewed without logging in.
- Added a `View app changelog` link to the login page so all players and visitors can access release notes.
- Logged-in players can still access Changelog from the Dashboard.
- App and Home Assistant version updated to `1.14.1`.

## [1.14.0] - 2026-08-21

### Added
- New in-app Changelog page available directly from the Dashboard.
- The page automatically reads the packaged `CHANGELOG.md`, so future release notes appear in the app without maintaining a second copy.
- Full version history is shown with Added, Changed, Fixed, Reliability and other release sections.
- The currently installed release is marked `CURRENT`.
- Changelog shortcut added to Admin.

### Changed
- App and Home Assistant version updated to `1.14.0`.

## [1.13.1] - 2026-08-21

### Changed
- Clarified the DP rule: only the single selected fixture's points are doubled; the player's total Gameweek score is never doubled.
- The 2-hour Signal reminder now also flags players who have not selected a DP match.
- A player can therefore appear in the final reminder for:
  - incomplete predictions
  - no DP selected
  - or both
- The 24-hour reminder remains focused on incomplete prediction cards.
- Manual reminder preview also shows missing DP status.
- App and Home Assistant version updated to `1.13.1`.

## [1.13.0] - 2026-08-21

### Added
- New `DP` (Double Points) rule.
- Each player can select one prediction per Gameweek as their DP match.
- The full normal score for the DP fixture is doubled:
  - correct winner: 3 → 6
  - correct draw: 4 → 8
  - exact winning score: 5 → 10
  - exact draw: 6 → 12
  - wrong result remains 0
- DP selector added to the Gameweek prediction page.
- DP badge added to revealed predictions in the Live Gameweek page.
- DP support added to Test Mode.
- Stats now show completed DPs used.

### Locking / Integrity
- A DP can be moved between open fixtures while the currently selected DP match has not kicked off.
- Once the selected DP fixture kicks off, the DP is locked for the rest of that Gameweek.
- DP selection is validated server-side and must belong to a saved prediction in that Gameweek.
- Database migration adds a persistent `dp` flag to existing live and Test Mode predictions without deleting existing data.

### Fixed
- Stats and leaderboard accuracy counts are now derived from prediction/result outcomes rather than raw point values, so doubled DP scores do not distort exact-score and correct-result statistics.

### Changed
- Rules page updated with DP scoring and examples.
- App and Home Assistant version updated to `1.13.0`.

## [1.12.1] - 2026-08-21

### Changed
- Prediction reminders now run twice per Gameweek when needed:
  - first reminder around 24 hours before the first kick-off
  - final reminder around 2 hours before the first kick-off
- Each reminder only lists players who still have incomplete predictions.
- If everyone is complete at either reminder point, no group message is sent.
- The 24-hour and 2-hour reminders have separate persistent sent-state to prevent duplicates.
- App and Home Assistant version updated to `1.12.1`.

## [1.12.0] - 2026-08-21

### Added
- Automatic Signal notification when a new Gameweek opens.
- Automatic one-time prediction reminder within 24 hours of the first kick-off.
- Reminder shows incomplete players and their submitted prediction count.
- Automatic Gameweek Results notification after the previous Gameweek completes.
- Results message includes Gameweek standings and updated overall standings.
- Individual Admin toggles for Gameweek Open, Reminder and Results notifications.
- Manual preview buttons for each notification type.
- Background notification checker runs every 15 minutes.

### Reliability
- Sent notification state is persisted in the Predictor database to avoid duplicate automatic messages after restarts.
- If all players are complete, the reminder is marked handled without sending unnecessary group noise.
- Automatic Signal errors are stored and displayed on the Signal Admin page.

### Changed
- App and Home Assistant version updated to `1.12.0`.

## [1.11.0] - 2026-08-21

### Added
- Admin → Signal configuration page.
- Signal REST API connection/status check.
- Persistent Signal settings.
- Enable/disable Signal toggle.
- Send Test Message button.
- Signal status card on Admin.
- Shared `send_signal_message()` function ready for automatic Gameweek notifications.

### Configured
- Existing working Signal endpoint and Predictor group are pre-populated.

## [1.10.0] - 2026-08-20

### Changed
- Each live fixture now locks individually at its own kick-off.
- Later fixtures in the same Gameweek remain editable until their own kick-off.
- Other players' predictions are revealed separately for each fixture once that match kicks off.
- Live points ignore future/unrevealed fixtures.
- App and Home Assistant version updated to `1.10.0`.

### Security / Integrity
- Prediction locking is enforced server-side.
- A second kick-off check is performed immediately before writing to the database.
- Late or manipulated submissions are ignored.

## [1.9.2] - 2026-08-20

### Changed
- Simplified Test Mode to exactly 5 fixtures.
- Each test player now submits exactly 5 predictions.
- Admin Test Mode page shows prediction forms for all four test players:
  - Fontz
  - Deludo
  - Tropic
  - Strat
- Normal players still only see their own assigned Test Mode identity.
- App and Home Assistant version updated to `1.9.2`.

### Fixed
- Admin can once again enter predictions for the other Test Mode users directly from the Test Mode page.

## [1.9.1] - 2026-08-20

### Changed
- Test Mode player names changed to `Fontz`, `Deludo`, `Tropic`, and `Strat`.
- Test aliases are isolated from live account display names.
- App version updated to `1.9.1`.

## [1.9.0] - 2026-08-20

### Added
- Admin toggle to make Test Mode available to normal players.
- Test Mode now uses each logged-in player's real account name.
- My Account page for players to edit their own name and PIN.

### Changed
- Each player can choose a maximum of 5 predictions from the 10 Test Mode fixtures.
- Admin still controls Test Gameweek creation, results and deletion.
- App version updated to `1.9.0`.

### Safety
- Test data remains isolated from the live league.
- Players cannot change their own admin role.
- Name/PIN changes preserve predictions, points and stats.

## [1.8.1] - 2026-08-20

### Changed
- Expanded Test Mode from 3 fixtures to a full 10-match fake Premier League Gameweek.
- Expanded Test Mode from 3 test players to 4:
  - Dan Test
  - Bob Test
  - Sarah Test
  - Mike Test
- Added more varied suggested results to exercise home wins, away wins, draws and exact-score bonuses.
- Improved Test Mode summary with fixture, player and finished-match counts.
- App and Home Assistant version updated to `1.8.1`.

### Safety
- Test Mode remains fully isolated from live league data.

## [1.8.0] - 2026-08-20

### Added
- Admin-only Test Mode with an isolated fake Gameweek.
- Three fake fixtures and three test players.
- Enter and edit test predictions before results are applied.
- Set fake final results and score them with the exact same `calculate_points()` function used by the live league.
- Test Gameweek leaderboard and per-match scoring audit.
- Finished test fixtures lock prediction editing.
- One-click deletion of all test data.

### Safety
- Test Mode uses dedicated `test_fixtures` and `test_predictions` tables.
- Test data never appears in the live Dashboard, History, Stats, fixtures, predictions or league totals.
- Deleting the Test Gameweek cannot delete live league data.

### Changed
- App and Home Assistant version updated to `1.8.0`.

## [1.7.5] - 2026-08-20

### Fixed
- Fixed Google OAuth token exchange failing with `invalid_grant: Missing code verifier`.
- Explicitly enables PKCE for the Google authorization flow.
- Persists the generated PKCE `code_verifier` before redirecting to Google.
- Sends the matching `code_verifier` to Google's token endpoint.
- Clears the saved OAuth state and verifier after successful authorization.

### Changed
- App and Home Assistant version updated to `1.7.5`.

## [1.7.4] - 2026-08-20

### Fixed
- Fixed a Python syntax error in the v1.7.3 Google OAuth callback diagnostics.
- Corrected escaped newline handling in OAuth state and token-exchange error messages.
- Validated the full `app.py` with Python compilation before packaging.

### Changed
- App and Home Assistant version updated to `1.7.4`.

## [1.7.3] - 2026-08-20

### Fixed
- Removed the Google OAuth callback's dependency on the Flask login session.
- OAuth state is now persisted in the Predictor database as well as the browser session.
- The callback validates against persistent state, so reverse-proxy/session-cookie issues cannot silently break the Google round trip.
- The callback now returns standalone HTML directly and does not depend on Jinja templates.
- Google Drive API calls are no longer made inside the OAuth callback; the callback only exchanges and saves tokens.

### Diagnostics
- Callback progress and state values are written to the addon log.
- Any callback exception is returned visibly in the browser with its Python exception type.

### Changed
- App and Home Assistant version updated to `1.7.3`.

## [1.7.2] - 2026-08-20

### Fixed
- Replaced the Google OAuth library callback token exchange with a direct request to Google's OAuth token endpoint.
- Added explicit HTTP timeout and token-response validation.
- Added a visible Google callback success/error page so OAuth failures no longer appear as a blank white page.
- Google callback failures are persisted as the last cloud backup error and written to the addon log.

### Changed
- App and Home Assistant version updated to `1.7.2`.

## [1.7.1] - 2026-08-20

### Fixed
- Fixed Google OAuth callback handling when the Predictor is behind an HTTPS reverse proxy.
- The callback now exchanges Google's returned authorization code directly instead of parsing the proxied callback URL.
- Added Werkzeug `ProxyFix` support for the external HTTPS scheme and host.
- Added explicit OAuth state checking and clearer callback error logging.

### Changed
- App and Home Assistant version updated to `1.7.1`.

## [1.7.0] - 2026-08-20

### Added
- Optional Google Drive backup integration.
- One-time Google OAuth connection from Admin > Backup & Restore.
- Automatic upload of each 6-hour database backup to Google Drive.
- Dedicated `Premier League Predictor Backups` folder created in Drive.
- Google Drive backup status and last successful cloud backup time.
- Manual `Back up to Drive now` action.
- Google Drive disconnect action.
- Cloud backup error display.

### Changed
- Reduced retained automatic local backups from 28 to 5.
- Google Drive backups are retained for 30 days.
- Manual local backups remain exempt from automatic pruning.
- Google OAuth access/refresh token is persisted in `/data/google_drive_token.json`.
- App and Home Assistant version updated to `1.7.0`.

### Security
- Google client credentials are entered through the Admin UI rather than hard-coded.
- The integration requests the limited Google Drive `drive.file` scope, which is intended for files created or opened by the app.

## [1.6.0] - 2026-08-20

### Added
- Admin player editing.
- Change a player's display name.
- Reset/change a player's PIN.
- Promote or demote players between Player and Admin roles.
- Dedicated Edit Player page.

### Safety
- Changing a player's name or PIN preserves all existing predictions, points and stats because the underlying player ID is unchanged.
- The currently logged-in administrator cannot accidentally remove their own admin access.
- Duplicate player names are blocked.

## [1.5.1] - 2026-08-20

### Fixed
- Fixed the missing `/history` route that caused a `404 Not Found` error.
- History is now included directly in the complete build rather than requiring a manual route patch.

### Changed
- Main dashboard now shows only the current gameweek.
- Added a dedicated Gameweek History page for previous rounds.
- Current gameweek automatically advances when the previous gameweek is fully finished.
- Updated in-app and Home Assistant app version to `1.5.1`.

## [1.4.0] - 2026-08-20

### Added
- Complete combined build with all routes pre-wired.
- Stats page.
- Last automatic backup shown in Admin.
- Last API refresh shown in Admin.
- App version displayed in Admin and footer.
- Automatic database backup every 6 hours.
- Backup & Restore admin page.
- Live Gameweek page with revealed predictions after kick-off.
- Live provisional Gameweek table.
- Player self-registration and admin registration toggle.
- Rules page.

### Changed
- Scoring system:
  - Correct draw: 4 points.
  - Correct winner: 3 points.
  - Exact score bonus: +2 points.
  - Exact draw: 6 points total.
  - Exact winning score: 5 points total.

## [1.3.0] - 2026-08-20

### Added
- Automatic SQLite backups every 6 hours.
- Retains the latest 28 automatic backups in `/data/backups`.

## [1.2.0] - 2026-08-20

### Changed
- Introduced result-based scoring plus exact-score bonus.

## [1.1.0] - 2026-08-20

### Added
- Dedicated Stats page and league records.

## [1.0.0] - 2026-08-20

### Added
- Backup and restore functionality.

## [0.9.0] - 2026-08-20

### Added
- Live Gameweek hub.
- Prediction reveal after kick-off.
- Live provisional Gameweek standings.

## [0.8.0] - 2026-08-20

### Added
- Complete combined build.
- Automatic fixture/result refresh.
- Persistent API token.

## [0.7.0] - 2026-08-20

### Added
- Rules page.

## [0.6.0] - 2026-08-20

### Added
- Player self-registration.

## [0.5.0] - 2026-08-20

### Changed
- Major mobile-first UI refresh.

## [0.4.0] - 2026-08-20

### Added
- Gameweek prediction entry.
- Kick-off locking.
- Leaderboard.

## [0.3.0] - 2026-08-20

### Added
- Football-data.org API integration.
- Premier League fixture import.

## [0.2.0] - 2026-08-20

### Added
- Player accounts using Name + PIN.
- Admin player management.

## [0.1.0] - 2026-08-20

### Added
- Initial Home Assistant app.
