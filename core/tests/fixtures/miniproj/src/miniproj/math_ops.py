def double(value: int) -> int:
    """Double an integer."""
    return value * 2


def clamp(value: int) -> int:
    """Clamp an integer into a display range."""
    if value < 0:
        return 0
    if value > 10:
        return 10
    return value


def normalize(value: int) -> int:
    """Return a normalized display value."""
    return clamp(double(value))


def format_value(value: int) -> str:
    """Format a normalized value."""
    normalized = normalize(value)
    return str(normalized)

