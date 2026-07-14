"""Module docstring for the documented fixture."""


def add(a, b):
    """Return the sum of a and b."""
    return a + b


def greet(name):
    """Greet someone.

    A longer description spanning
    multiple lines.
    """
    return f"hello {name}"


def undocumented(x):
    return x * 2


class Widget:
    """A small widget."""

    def spin(self):
        """Spin it."""
        return "spin"
