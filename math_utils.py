#!/usr/bin/env python3
"""数学工具模块，提供素数判断和最大公约数计算功能。"""

import math


def is_prime(n: int) -> bool:
    """判断一个整数是否为素数。

    素数是大于 1 的自然数，且只能被 1 和自身整除。
    使用试除法，仅检查到 sqrt(n)，时间复杂度 O(√n)。

    参数:
        n: 待判断的整数，可以是负数、零或正整数。

    返回:
        True 如果 n 是素数，否则返回 False。

    示例:
        >>> is_prime(2)
        True
        >>> is_prime(4)
        False
        >>> is_prime(1)
        False
        >>> is_prime(-7)
        False
    """
    if n <= 1:
        return False
    if n <= 3:
        return True
    if n % 2 == 0 or n % 3 == 0:
        return False

    # 从 5 开始，跳过 2 和 3 的倍数，仅检查形如 6k±1 的因子
    limit = int(math.isqrt(n))
    for i in range(5, limit + 1, 6):
        if n % i == 0 or n % (i + 2) == 0:
            return False
    return True


def gcd(a: int, b: int) -> int:
    """使用欧几里得算法（辗转相除法）计算两个整数的最大公约数。

    返回能同时整除 a 和 b 的最大正整数。
    对于任意整数（包括零和负数），结果始终为非负整数。

    参数:
        a: 第一个整数。
        b: 第二个整数。

    返回:
        a 和 b 的最大公约数（非负）。

    示例:
        >>> gcd(48, 18)
        6
        >>> gcd(0, 5)
        5
        >>> gcd(-48, 18)
        6
        >>> gcd(7, 13)
        1
    """
    a, b = abs(a), abs(b)
    while b != 0:
        a, b = b, a % b
    return a


def main() -> None:
    """运行 is_prime 和 gcd 的测试用例。"""
    print("=== is_prime 测试 ===")
    test_cases_prime = [
        (2, True),
        (3, True),
        (4, False),
        (5, True),
        (1, False),
        (0, False),
        (-7, False),
        (97, True),
        (100, False),
        (7919, True),  # 第 1000 个素数
    ]
    for n, expected in test_cases_prime:
        result = is_prime(n)
        status = "✓" if result == expected else "✗"
        print(f"  {status} is_prime({n:>5}) = {result}  (期望: {expected})")

    print("\n=== gcd 测试 ===")
    test_cases_gcd = [
        (48, 18, 6),
        (0, 5, 5),
        (5, 0, 5),
        (-48, 18, 6),
        (7, 13, 1),
        (100, 100, 100),
        (-12, -8, 4),
        (0, 0, 0),
        (1071, 462, 21),  # 欧几里得算法经典例子
    ]
    for a, b, expected in test_cases_gcd:
        result = gcd(a, b)
        status = "✓" if result == expected else "✗"
        print(f"  {status} gcd({a:>5}, {b:>5}) = {result}  (期望: {expected})")

    all_pass = all(
        is_prime(n) == expected for n, expected in test_cases_prime
    ) and all(
        gcd(a, b) == expected for a, b, expected in test_cases_gcd
    )
    print(f"\n{'所有测试通过' if all_pass else '存在失败用例'}")
    if not all_pass:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
