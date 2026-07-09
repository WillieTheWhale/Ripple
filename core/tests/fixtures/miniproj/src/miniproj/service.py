from miniproj.math_ops import format_value, normalize


class Processor:
    def run(self, value: int) -> int:
        """Run the processor."""
        return normalize(value)

    def label(self, value: int) -> str:
        """Build a label."""
        return format_value(value)


def compute_value(value: int) -> int:
    """Process a value."""
    return normalize(value)


def compute_label(value: int) -> str:
    """Process and format a value."""
    return format_value(value)
