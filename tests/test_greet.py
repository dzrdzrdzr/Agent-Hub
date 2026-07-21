"""Acceptance test for greet(name) function."""

import pytest


def greet(name: str) -> str:
    """Return a greeting string for the given name.

    Args:
        name: The name to greet.

    Returns:
        A greeting message.

    Raises:
        TypeError: If name is not a string.
        ValueError: If name is empty or only whitespace.
    """
    if not isinstance(name, str):
        raise TypeError(f"name must be str, got {type(name).__name__}")
    stripped = name.strip()
    if not stripped:
        raise ValueError("name must not be empty")
    return f"Hello, {stripped}!"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_greet_basic():
    """greet returns expected string for a normal name."""
    assert greet("World") == "Hello, World!"


def test_greet_with_whitespace():
    """greet strips leading/trailing whitespace."""
    assert greet("  Alice  ") == "Hello, Alice!"


def test_greet_empty_raises():
    """greet raises ValueError on empty string."""
    with pytest.raises(ValueError, match="must not be empty"):
        greet("")


def test_greet_whitespace_only_raises():
    """greet raises ValueError on whitespace-only string."""
    with pytest.raises(ValueError, match="must not be empty"):
        greet("   ")


def test_greet_type_error():
    """greet raises TypeError for non-string input."""
    with pytest.raises(TypeError, match="name must be str"):
        greet(42)


def test_greet_none_raises():
    """greet raises TypeError for None."""
    with pytest.raises(TypeError, match="name must be str"):
        greet(None)
