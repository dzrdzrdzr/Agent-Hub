#!/usr/bin/env python3
"""数学扩展模块，提供阶乘计算和斐波那契数列生成功能。"""


def factorial(n: int) -> int:
    """使用递归方式计算 n 的阶乘（n!）。

    阶乘定义为 n! = n × (n-1) × ... × 2 × 1，其中 0! = 1。
    递归公式: factorial(n) = n × factorial(n-1)，基准条件 factorial(0) = 1。

    参数:
        n: 非负整数。

    返回:
        n 的阶乘值。

    异常:
        ValueError: 当 n 为负数时抛出，阶乘仅对非负整数有定义。
        RecursionError: 当 n 过大（通常 > 998）时，递归深度超出 Python 默认限制。

    示例:
        >>> factorial(0)
        1
        >>> factorial(1)
        1
        >>> factorial(5)
        120
        >>> factorial(10)
        3628800
    """
    if not isinstance(n, int):
        raise TypeError(f"factorial 要求整数类型，实际传入 {type(n).__name__}")
    if n < 0:
        raise ValueError(f"阶乘仅对非负整数有定义，实际传入 {n}")
    if n == 0:
        return 1
    return n * factorial(n - 1)


def fibonacci(n: int) -> list[int]:
    """生成斐波那契数列的前 n 项。

    斐波那契数列定义:
        F(0) = 0, F(1) = 1
        F(k) = F(k-1) + F(k-2)  (k >= 2)

    参数:
        n: 要生成的项数，非负整数。

    返回:
        长度为 n 的列表，包含斐波那契数列的前 n 项。

    异常:
        ValueError: 当 n 为负数时抛出。

    示例:
        >>> fibonacci(0)
        []
        >>> fibonacci(1)
        [0]
        >>> fibonacci(2)
        [0, 1]
        >>> fibonacci(5)
        [0, 1, 1, 2, 3]
        >>> fibonacci(10)
        [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]
    """
    if not isinstance(n, int):
        raise TypeError(f"fibonacci 要求整数类型，实际传入 {type(n).__name__}")
    if n < 0:
        raise ValueError(f"项数不能为负数，实际传入 {n}")
    if n == 0:
        return []
    if n == 1:
        return [0]

    result = [0, 1]
    for _ in range(2, n):
        result.append(result[-1] + result[-2])
    return result


def main() -> None:
    """运行 factorial 和 fibonacci 的测试用例。"""
    print("=== factorial 测试 ===")
    test_cases_factorial = [
        (0, 1),
        (1, 1),
        (2, 2),
        (5, 120),
        (10, 3628800),
        (6, 720),
    ]
    for n, expected in test_cases_factorial:
        result = factorial(n)
        status = "✓" if result == expected else "✗"
        print(f"  {status} factorial({n}) = {result}  (期望: {expected})")

    print("\n=== factorial 边界测试 ===")
    try:
        factorial(-1)
        print("  ✗ factorial(-1) 应该抛出 ValueError")
    except ValueError as e:
        print(f"  ✓ factorial(-1) 正确抛出 ValueError: {e}")

    print("\n=== fibonacci 测试 ===")
    test_cases_fibonacci = [
        (0, []),
        (1, [0]),
        (2, [0, 1]),
        (5, [0, 1, 1, 2, 3]),
        (10, [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]),
    ]
    for n, expected in test_cases_fibonacci:
        result = fibonacci(n)
        status = "✓" if result == expected else "✗"
        print(f"  {status} fibonacci({n}) = {result}")
        if result != expected:
            print(f"      期望: {expected}")

    print("\n=== fibonacci 边界测试 ===")
    try:
        fibonacci(-1)
        print("  ✗ fibonacci(-1) 应该抛出 ValueError")
    except ValueError as e:
        print(f"  ✓ fibonacci(-1) 正确抛出 ValueError: {e}")

    # 汇总
    all_pass = True
    for n, expected in test_cases_factorial:
        if factorial(n) != expected:
            all_pass = False
    for n, expected in test_cases_fibonacci:
        if fibonacci(n) != expected:
            all_pass = False

    print(f"\n{'所有测试通过' if all_pass else '存在失败用例'}")
    if not all_pass:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
