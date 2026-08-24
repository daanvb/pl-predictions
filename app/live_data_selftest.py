from pathlib import Path

import football_api


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def fake_get(url, headers=None, params=None, timeout=None):
    if params and params.get("status") == "LIVE":
        return FakeResponse({
            "matches": [{
                "id": 101,
                "status": "LIVE",
                "score": {
                    "fullTime": {
                        "homeTeam": 2,
                        "awayTeam": 1,
                    }
                },
            }]
        })

    return FakeResponse({
        "matches": [{
            "id": 101,
            "status": "TIMED",
            "score": {
                "fullTime": {
                    "home": None,
                    "away": None,
                }
            },
        }]
    })


original_get = football_api.requests.get
original_season_helper = football_api._current_season_start_year

try:
    football_api.requests.get = fake_get
    football_api._current_season_start_year = lambda: 2026

    matches = football_api.get_matches("test-token", season=2026)
finally:
    football_api.requests.get = original_get
    football_api._current_season_start_year = original_season_helper

assert len(matches) == 1
assert matches[0]["status"] == "IN_PLAY"
assert matches[0]["score"]["fullTime"]["home"] == 2
assert matches[0]["score"]["fullTime"]["away"] == 1

# Historical seasons must not make the optional live-overlay request.
calls = []


def historical_fake_get(url, headers=None, params=None, timeout=None):
    calls.append(dict(params or {}))
    return FakeResponse({"matches": []})

football_api.requests.get = historical_fake_get
football_api._current_season_start_year = lambda: 2026

try:
    football_api.get_matches("test-token", season=2025)
finally:
    football_api.requests.get = original_get
    football_api._current_season_start_year = original_season_helper

assert len(calls) == 1
assert "status" not in calls[0]

# Dashboard must render any available running score instead of waiting for FT,
# and its submeta must use the status helper so kickoff fallbacks are visible.
template = Path(__file__).with_name("templates").joinpath("dashboard.html").read_text(
    encoding="utf-8"
)

assert "fixture.home_score is not none and fixture.away_score is not none" in template
assert "fixture.status == 'FINISHED' and fixture.home_score" not in template
assert "<div class=\"fixture-submeta\">\n{{ status_label(fixture) }}\n</div>" in template

print("live data self-test passed")
