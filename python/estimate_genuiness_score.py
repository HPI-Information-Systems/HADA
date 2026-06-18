from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class FunctionalDependency:
    lhs: tuple[str, ...]
    rhs: tuple[str, ...]


class GenuinenessEstimator:
    """Estimates the probability that a possible world drawn from a probabilistic
    dataset satisfies a functional dependency `fd.lhs -> fd.rhs`.

    Each row of the dataset D is a dict with one key per `fd.lhs` attribute plus a
    "distribution" key: {rhs_tuple: probability, ...}, where rhs_tuple lines up
    positionally with `fd.rhs`. Probabilities for a row must sum to 1.

    This implements Algorithm 1 ("Estimate Genuineness Score"): the score is the
    probability that, if every ambiguous row independently redrew its RHS value
    from its own belief distribution, the FD `lhs -> rhs` would still hold across
    the entire dataset.
    """

    def __init__(self, fd, dataset=None):
        self.fd = fd
        self.dataset = dataset

    def estimate(self, D):
        C = self._extract_constraints(D)
        return self._estimate(D, C, 1.0)

    def _extract_constraints(self, D):
        C = defaultdict(set)

        for t in D:
            dist = t["distribution"]

            # deterministic tuple (probability mass = 1)
            if len(dist) == 1:
                rhs, _ = next(iter(dist.items()))
                lhs = self._project(t, self.fd.lhs)
                C[lhs].add(rhs)

        return C

    # Implemented as an explicit stack instead of recursion (Python's default
    # recursion-depth limit is too low for datasets with hundreds of thousands
    # of rows) and multiplies probabilities going down the stack rather than
    # multiplying the recursive result on the way back up - both give the same
    # number since multiplication distributes over the sum either way.
    def _estimate(self, D, C, prob):
        total = 0.0
        stack = [(0, C, prob)]

        while stack:
            index, C_cur, prob_cur = stack.pop()

            if index == len(D):
                total += prob_cur
                continue

            t = D[index]
            lhs = self._project(t, self.fd.lhs)
            distribution = t["distribution"]

            if len(distribution) == 1:
                rhs, _ = next(iter(distribution.items()))
                if not self._violates(lhs, rhs, C_cur):
                    stack.append((index + 1, C_cur, prob_cur))
                continue

            for rhs, p in distribution.items():
                if self._violates(lhs, rhs, C_cur):
                    continue

                new_C = defaultdict(set, {k: set(v) for k, v in C_cur.items()})
                new_C[lhs].add(rhs)

                stack.append((index + 1, new_C, prob_cur * p))

        return total

    def _project(self, row, attrs):
        return tuple(row[a] for a in attrs)

    def _violates(self, lhs, rhs, C):
        if lhs not in C:
            return False
        return len(C[lhs]) > 0 and rhs not in C[lhs]


@dataclass(frozen=True)
class OrderDependency:
    lhs: tuple[str, ...]
    rhs: tuple[str, ...]


class OdGenuinenessEstimator:
    """Estimates the probability that a possible world drawn from a probabilistic
    dataset satisfies an order dependency `od.lhs |=> od.rhs`: sorting by lhs
    implies the rhs tuple is lexicographically non-decreasing - with ties in lhs
    forced to an identical rhs tuple, i.e. an FD embedded between each lhs group.

    `groups` must be a list of (lhs_value, distribution) pairs already sorted by
    ascending lhs_value, where `distribution` is {rhs_tuple: probability} - the
    probability that every row sharing that lhs_value independently redraws to
    the SAME rhs_tuple (the within-group FD piece, already marginalized by the
    caller; see DependencyDiscoveryRunner.compute_od_genuineness for how this is
    built from raw per-row counts).

    Because `<=` is transitive, monotonicity across the whole sorted sequence of
    groups only needs to be checked between consecutive groups - so unlike the FD
    case (independent groups, multiplied), this chains groups via a DP over "the
    rhs_tuple committed to by the previous group" instead of multiplying isolated
    per-group scores.
    """

    def __init__(self, od):
        self.od = od

    def estimate(self, groups):
        # dp[v] = probability that every group processed so far committed to
        # tuples forming a non-decreasing sequence ending in v. `None` is the
        # sentinel "no previous group yet" (acts as -infinity).
        dp = {None: 1.0}

        for _, distribution in groups:
            new_dp = defaultdict(float)
            for prev_value, prev_prob in dp.items():
                for value, p in distribution.items():
                    if prev_value is None or value >= prev_value:
                        new_dp[value] += prev_prob * p
            dp = new_dp

        return sum(dp.values())
