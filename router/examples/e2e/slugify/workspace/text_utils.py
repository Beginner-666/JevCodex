import re


def slugify(value: str) -> str:
    """Convert text to a stable ASCII slug.

    Strip leading/trailing whitespace, lowercase ASCII letters, and replace every
    non-empty run of characters other than ASCII letters or digits with one hyphen.
    The result must not start or end with a hyphen. Return an empty string when no
    letters or digits remain.
    """
    raise NotImplementedError
