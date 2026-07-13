"""Tiny module governed by fixture docs."""


def add(a, b):
    return a + b


def scaled_add(a, b, factor):
    # Calls add: creates an IMPORTS/CALLS edge for blast-radius tests.
    return add(a, b) * factor


class Accumulator:
    def __init__(self):
        self.total = 0

    def push(self, value):
        self.total = add(self.total, value)
        return self.total
