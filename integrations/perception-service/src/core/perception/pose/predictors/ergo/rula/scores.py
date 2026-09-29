"""RULA scoring functions and lookup tables.

Reference: McAtamney & Corlett (1993), "RULA: a survey method for the
investigation of work-related upper limb disorders."
"""

from core.models.pose import RiskLevel


def score_upper_arm(angle: float) -> int:
    """+1: [-20,20], +2: <-20 or (20,45], +3: (45,90], +4: >90."""
    if -20 <= angle <= 20:
        return 1
    elif angle < -20 or 20 < angle <= 45:
        return 2
    elif 45 < angle <= 90:
        return 3
    else:
        return 4


def score_lower_arm(angle: float) -> int:
    """+1: [60,100], +2: <60 or >100."""
    if 60 <= angle <= 100:
        return 1
    else:
        return 2


def score_wrist(angle: float) -> int:
    """+1: neutral, +2: <=15, +3: >15."""
    abs_angle: float = abs(angle)
    if abs_angle <= 1:
        return 1
    elif abs_angle <= 15:
        return 2
    else:
        return 3


def score_neck(angle: float) -> int:
    """+1: [0,10], +2: (10,20], +3: >20, +4: extension (<0)."""
    if angle < 0:
        return 4
    elif angle <= 10:
        return 1
    elif angle <= 20:
        return 2
    else:
        return 3


def score_trunk(angle: float) -> int:
    """+1: ~0, +2: (0,20], +3: (20,60], +4: >60."""
    abs_angle: float = abs(angle)
    if abs_angle <= 1:
        return 1
    elif abs_angle <= 20:
        return 2
    elif abs_angle <= 60:
        return 3
    else:
        return 4


# Lookup tables (McAtamney & Corlett 1993). Table A: [upper_arm-1][lower_arm-1][wrist-1][wrist_twist-1]
TABLE_A: list[list[list[list[int]]]] = [
    [
        [[1, 2], [2, 2], [2, 3], [3, 3]],
        [[2, 2], [2, 2], [3, 3], [3, 3]],
        [[2, 3], [3, 3], [3, 3], [4, 4]],
    ],
    [
        [[2, 3], [3, 3], [3, 4], [4, 4]],
        [[3, 3], [3, 3], [3, 4], [4, 4]],
        [[3, 4], [4, 4], [4, 4], [5, 5]],
    ],
    [
        [[3, 3], [4, 4], [4, 4], [5, 5]],
        [[3, 4], [4, 4], [4, 4], [5, 5]],
        [[4, 4], [4, 4], [4, 5], [5, 5]],
    ],
    [
        [[4, 4], [4, 4], [4, 5], [5, 5]],
        [[4, 4], [4, 4], [4, 5], [5, 5]],
        [[4, 4], [4, 5], [5, 5], [6, 6]],
    ],
    [
        [[5, 5], [5, 5], [5, 6], [6, 7]],
        [[5, 6], [6, 6], [6, 7], [7, 7]],
        [[6, 6], [6, 7], [7, 7], [7, 8]],
    ],
    [
        [[7, 7], [7, 7], [7, 8], [8, 9]],
        [[8, 8], [8, 8], [8, 9], [9, 9]],
        [[9, 9], [9, 9], [9, 9], [9, 9]],
    ],
]

# Table B: [neck-1][trunk-1][legs-1]
TABLE_B: list[list[list[int]]] = [
    [[1, 3], [2, 3], [3, 4], [5, 5], [6, 6], [7, 7]],
    [[2, 3], [2, 3], [4, 5], [5, 5], [6, 7], [7, 7]],
    [[3, 3], [3, 4], [4, 5], [5, 6], [6, 7], [7, 7]],
    [[5, 5], [5, 6], [6, 7], [7, 7], [7, 7], [8, 8]],
    [[7, 7], [7, 7], [7, 8], [8, 8], [8, 8], [8, 8]],
    [[8, 8], [8, 8], [8, 8], [8, 9], [9, 9], [9, 9]],
]

# Table C: [score_a-1][score_b-1]
TABLE_C: list[list[int]] = [
    [1, 2, 3, 3, 4, 5, 5],
    [2, 2, 3, 4, 4, 5, 5],
    [3, 3, 3, 4, 4, 5, 6],
    [3, 3, 3, 4, 5, 6, 6],
    [4, 4, 4, 5, 6, 7, 7],
    [4, 4, 5, 6, 6, 7, 7],
    [5, 5, 6, 6, 7, 7, 7],
    [5, 5, 6, 7, 7, 7, 7],
]


def lookup_table_a(upper_arm: int, lower_arm: int, wrist: int, wrist_twist: int) -> int:
    ua: int = min(max(upper_arm, 1), 6) - 1
    la: int = min(max(lower_arm, 1), 3) - 1
    w: int = min(max(wrist, 1), 4) - 1
    wt: int = min(max(wrist_twist, 1), 2) - 1
    return TABLE_A[ua][la][w][wt]


def lookup_table_b(neck: int, trunk: int, legs: int) -> int:
    n: int = min(max(neck, 1), 6) - 1
    t: int = min(max(trunk, 1), 6) - 1
    lg: int = min(max(legs, 1), 2) - 1
    return TABLE_B[n][t][lg]


def lookup_table_c(score_a: int, score_b: int) -> int:
    sa: int = min(max(score_a, 1), 8) - 1
    sb: int = min(max(score_b, 1), 7) - 1
    return TABLE_C[sa][sb]


def risk_level_from_score(score: int) -> RiskLevel:
    if score <= 2:
        return RiskLevel.NEGLIGIBLE
    elif score <= 4:
        return RiskLevel.LOW
    elif score <= 6:
        return RiskLevel.MEDIUM
    else:
        return RiskLevel.HIGH
