"""Module with a decorated symbol for region-span tests."""
from functools import lru_cache


@lru_cache(maxsize=1)
def cached(n):
    return n * 2
