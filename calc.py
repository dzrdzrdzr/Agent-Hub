"""Basic arithmetic operations module.

Provides four core arithmetic functions: add, subtract, multiply, and divide.
All functions accept numeric inputs and return numeric results.
"""

import logging
from typing import Union

logger = logging.getLogger(__name__)

Number = Union[int, float]


def add(a: Number, b: Number) -> Number:
    """Return the sum of two numbers.

    Args:
        a: The first operand.
        b: The second operand.

    Returns:
        The result of a + b.

    Examples:
        >>> add(3, 5)
        8
        >>> add(-1, 2.5)
        1.5
    """
    return a + b


def subtract(a: Number, b: Number) -> Number:
    """Return the difference between two numbers.

    Args:
        a: The minuend.
        b: The subtrahend.

    Returns:
        The result of a - b.

    Examples:
        >>> subtract(10, 3)
        7
        >>> subtract(0, 5)
        -5
    """
    return a - b


def multiply(a: Number, b: Number) -> Number:
    """Return the product of two numbers.

    Args:
        a: The first factor.
        b: The second factor.

    Returns:
        The result of a * b.

    Examples:
        >>> multiply(3, 5)
        15
        >>> multiply(-2, 0)
        0
    """
    return a * b


def divide(a: Number, b: Number) -> float:
    """Return the quotient of two numbers.

    Args:
        a: The dividend (numerator).
        b: The divisor (denominator).

    Returns:
        The result of a / b as a float.

    Raises:
        ZeroDivisionError: If the divisor ``b`` is zero.

    Examples:
        >>> divide(10, 2)
        5.0
        >>> divide(7, 2)
        3.5
    """
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b


def main() -> None:
    """Run built-in smoke tests to verify the arithmetic functions."""
    # Test: 3 + 5 == 8
    result_add = add(3, 5)
    assert result_add == 8, f"add(3, 5) expected 8, got {result_add}"
    logger.info("add(3, 5) = %s  (passed)", result_add)

    # Test: 10 / 2 == 5
    result_div = divide(10, 2)
    assert result_div == 5.0, f"divide(10, 2) expected 5.0, got {result_div}"
    logger.info("divide(10, 2) = %s  (passed)", result_div)

    logger.info("All tests passed.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    main()
