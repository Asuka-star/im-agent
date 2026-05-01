def coerce_positive_int(value: object, *, default: int = 1) -> int:
    try:
        return max(int(value or default), 1)
    except (TypeError, ValueError):
        return max(default, 1)
