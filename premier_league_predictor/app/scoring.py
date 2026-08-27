def calculate_points(predicted_home, predicted_away, actual_home, actual_away):
    """
    Scoring:
      Correct draw = 4 points
      Correct winner = 3 points
      Exact score = +2 bonus

    Totals:
      Exact draw = 6
      Correct draw, wrong score = 4
      Exact winning score = 5
      Correct winner, wrong score = 3
      Wrong result = 0
    """
    if actual_home is None or actual_away is None:
        return 0

    predicted_draw = predicted_home == predicted_away
    actual_draw = actual_home == actual_away
    exact_score = (
        predicted_home == actual_home
        and predicted_away == actual_away
    )

    if actual_draw:
        if not predicted_draw:
            return 0
        return 4 + (2 if exact_score else 0)

    if predicted_draw:
        return 0

    predicted_home_win = predicted_home > predicted_away
    actual_home_win = actual_home > actual_away

    if predicted_home_win != actual_home_win:
        return 0

    return 3 + (2 if exact_score else 0)



def calculate_prediction_points(
    predicted_home,
    predicted_away,
    actual_home,
    actual_away,
    double_points=False,
):
    """
    Calculate the final points awarded for a prediction.

    DP doubles the complete points award, including any exact-score bonus:
      correct winner        3 -> 6
      correct draw          4 -> 8
      exact winning score   5 -> 10
      exact draw            6 -> 12
      wrong result          0 -> 0
    """
    base_points = calculate_points(
        predicted_home,
        predicted_away,
        actual_home,
        actual_away,
    )

    return base_points * 2 if double_points else base_points
