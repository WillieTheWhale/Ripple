from miniproj.math_ops import normalize


class Base:
    def identity(self, value: int) -> int:
        """Return the value unchanged."""
        return value


class Child(Base):
    def compute(self, value: int) -> int:
        """Compute through the shared normalizer."""
        return normalize(value)

