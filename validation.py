"""Input normalization for queue challenge configuration.

This module intentionally has no CTFd or Flask dependency so the validation
rules can be exercised independently in the plugin's CI suite.
"""


def normalize_queue_fields(data, error_type, partial=False):
    """Normalize and validate fields accepted by QueueChallenge create/update."""
    # CTFd's dynamic form serialization can include a control with an empty
    # name. SQLAlchemy's declarative constructor rejects it as an unknown key.
    result = {
        key: value
        for key, value in dict(data).items()
        if isinstance(key, str) and key.strip()
    }

    if not partial or "queue_mode" in result:
        mode = str(result.get("queue_mode", "manual")).strip().lower()
        if mode not in {"manual", "automatic"}:
            raise error_type("Queue mode must be manual or automatic.")
        result["queue_mode"] = mode

    if not partial or "function" in result:
        scoring_function = str(result.get("function", "static")).strip().lower()
        if scoring_function not in ("static", "linear", "logarithmic"):
            raise error_type(
                "Scoring function must be static, linear or logarithmic."
            )
        result["function"] = scoring_function

    for key, default, minimum, maximum in (
        ("batch_size", 1, 1, 1000),
        ("interval_seconds", 900, 1, 86400),
    ):
        if partial and key not in result:
            continue

        raw_value = result.get(key, default)
        try:
            if isinstance(raw_value, bool):
                raise ValueError("boolean is not an integer")
            if isinstance(raw_value, float) and not raw_value.is_integer():
                raise ValueError("fractional values are not allowed")
            value = int(raw_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise error_type("{} must be an integer.".format(key)) from exc

        if not minimum <= value <= maximum:
            raise error_type(
                "{} must be between {} and {}.".format(key, minimum, maximum)
            )
        result[key] = value

    for key, default in (("show_position", True), ("show_eta", True)):
        if not partial or key in result:
            value = result.get(key, default)
            if isinstance(value, bool):
                result[key] = value
            else:
                result[key] = str(value).strip().lower() in (
                    "1", "true", "yes", "on"
                )

    if not partial or "selected_message" in result:
        raw_message = result.get(
            "selected_message",
            "You have been selected! You may now attempt this challenge.",
        )
        if raw_message is None:
            message = ""
        elif isinstance(raw_message, str):
            message = raw_message
        else:
            raise error_type("The selected-participant message must be text.")
        if len(message) > 4000:
            raise error_type(
                "The selected-participant message must be 4000 characters or fewer."
            )
        result["selected_message"] = message

    return result
