# Premier League Predictor

Home Assistant app repository for the Premier League Predictor.

## Install in Home Assistant

Add this repository URL in Home Assistant's app store repositories:

`https://github.com/daanvb/pl-predictions`

The app is stored in `premier_league_predictor/`.

## Existing local installation

If you currently run this as a Local app/add-on, make a Home Assistant backup and an in-app database backup before switching to the GitHub repository build. Home Assistant identifies Local and GitHub repository apps differently, so verify your Predictor data before removing the existing Local installation.

On a fresh GitHub installation with no players, open the app and upload the
existing Predictor `.db` backup on the first-run restore screen. The upload is
integrity-checked and must contain a compatible schema, at least one player,
and an administrator. Enter the one-time restore code shown in the new app's
log. The code and restore screen disable themselves as soon as users exist.
Keep the old Local installation until the restored copy is verified.
