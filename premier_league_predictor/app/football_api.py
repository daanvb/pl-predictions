import requests

API_BASE = "https://api.football-data.org/v4"
COMPETITION = "PL"


class FootballAPIError(Exception):
    pass


def headers(token):
    return {
        "X-Auth-Token": token,
        "Accept": "application/json",
    }


def test_connection(token):
    if not token:
        raise FootballAPIError("No API token has been configured.")

    response = requests.get(
        f"{API_BASE}/competitions/{COMPETITION}",
        headers=headers(token),
        timeout=15,
    )

    if response.status_code == 200:
        return response.json()

    if response.status_code == 401:
        raise FootballAPIError("API token was rejected.")

    if response.status_code == 403:
        raise FootballAPIError(
            "Your API account does not have permission to access this resource."
        )
    if response.status_code == 429:
        raise FootballAPIError(
            "football-data.org is rate limiting requests. Please try again shortly."
        )

    raise FootballAPIError(
        f"Football API returned HTTP {response.status_code}"
    )


def get_competition_matches(token, competition, season=2026):
    if not token:
        raise FootballAPIError("No API token has been configured.")

    params = {"limit": 500}
    if season is not None:
        params["season"] = season

    response = requests.get(
        f"{API_BASE}/competitions/{competition}/matches",
        headers=headers(token),
        params=params,
        timeout=30,
    )

    if response.status_code == 401:
        raise FootballAPIError("API token was rejected.")

    if response.status_code == 403:
        raise FootballAPIError(
            "Your API account does not have permission to retrieve these fixtures."
        )
    if response.status_code == 429:
        raise FootballAPIError(
            "football-data.org is rate limiting requests. Please try again shortly."
        )

    if response.status_code != 200:
        raise FootballAPIError(
            f"Football API returned HTTP {response.status_code}"
        )

    return response.json().get("matches", [])


def get_matches(token, season=2026):
    return get_competition_matches(token, COMPETITION, season)


def get_champions_league_matches(token, season=2026):
    """Try the documented CL code, numeric ID and provider-current season."""
    attempts = (("CL", season), (2001, season), (2001, None))
    last_error = None
    for competition, requested_season in attempts:
        try:
            return get_competition_matches(
                token, competition, requested_season
            )
        except FootballAPIError as exc:
            last_error = exc
            if "HTTP 404" not in str(exc):
                raise
    raise FootballAPIError(
        "Champions League data is not available from the configured "
        "football-data.org account."
    ) from last_error


def get_match(token, match_id):
    if not token:
        raise FootballAPIError("No API token has been configured.")

    try:
        response = requests.get(
            f"{API_BASE}/matches/{match_id}",
            headers=headers(token),
            timeout=15,
        )
    except requests.RequestException as exc:
        raise FootballAPIError(
            "Match scorer details are temporarily unavailable."
        ) from exc

    if response.status_code == 401:
        raise FootballAPIError("API token was rejected.")

    if response.status_code == 403:
        raise FootballAPIError(
            "Your API account cannot retrieve match event details."
        )
    if response.status_code == 429:
        raise FootballAPIError(
            "football-data.org is rate limiting requests. Please try again shortly."
        )

    if response.status_code != 200:
        raise FootballAPIError(
            f"Football API returned HTTP {response.status_code}"
        )

    return response.json()
