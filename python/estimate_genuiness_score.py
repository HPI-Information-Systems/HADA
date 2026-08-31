from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FunctionalDependency:
    lhs: tuple[str, ...]
    rhs: tuple[str, ...]


@dataclass
class FdStats:
    """Statistics about a violating dependency candidate `dep.lhs -> dep.rhs`
    (an FD or an OD), gathered from the live data. Different GenuinenessScore
    strategies each need a different subset of these fields - callers only
    have to fill in what their chosen strategy actually reads.
    """

    # lhs_value -> {rhs_value: row_count}, used by ProbabilisticGenuinenessScore.
    rhs_counts_by_lhs: Optional[dict] = None

    # |R|: total number of attributes in the relation that the dependency belongs to.
    relation_attribute_count: Optional[int] = None
    # 1-based ordinal positions of the lhs/rhs attributes within the relation,
    # in the same order as dep.lhs/dep.rhs.
    lhs_positions: Optional[tuple] = None
    rhs_positions: Optional[tuple] = None
    # |max(X)|: length of the longest value among the lhs attribute(s).
    lhs_max_value_length: Optional[int] = None
    # uniques()/values() for the lhs and rhs attribute(s). Currently unused -
    # only needed if PapenbrockScore's duplication score is re-enabled.
    lhs_unique_count: Optional[int] = None
    lhs_value_count: Optional[int] = None
    rhs_unique_count: Optional[int] = None
    rhs_value_count: Optional[int] = None


class GenuinenessScore(ABC):
    """Common interface for genuineness-scoring strategies: given a violating
    dependency (FD or OD) and the FdStats gathered for it, return a score
    estimating how likely the dependency is to be real and semantically
    meaningful rather than a coincidence of the current data.
    """

    @abstractmethod
    def score(self, dependency, stats):
        ...


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


class ProbabilisticGenuinenessScore(GenuinenessScore):
    """Wraps GenuinenessEstimator (Algorithm 1) behind the GenuinenessScore
    interface: the score is the probability that the FD would still hold if
    every row sharing an ambiguous lhs value independently redrew its rhs
    value from the empirical distribution observed for that lhs value.
    """

    def score(self, dependency, stats):
        estimator = GenuinenessEstimator(dependency)

        score = 1.0
        for lhs_value, rhs_counts in stats.rhs_counts_by_lhs.items():
            total = sum(rhs_counts.values())
            distribution = {rhs: cnt / total for rhs, cnt in rhs_counts.items()}
            row = dict(zip(dependency.lhs, lhs_value))
            group_rows = [{**row, "distribution": distribution} for _ in range(total)]
            score *= estimator.estimate(group_rows)

        return score


class PapenbrockScore(GenuinenessScore):
    """Scores a violating dependency `X -> Y` (an FD or an OD) as a good
    foreign-key candidate, following the "Violating FD selection" heuristic of
    Papenbrock et al., "Data-driven Schema Normalization" (2017), Section 7.2.
    The same features apply unchanged to ODs: their Lhs/Rhs become the same
    kind of foreign-key candidate after decomposition.

    Unlike ProbabilisticGenuinenessScore, this does not estimate the probability
    that the dependency provably holds - it scores how semantically plausible a
    real dependency of this shape looks (short, frequently-determining Lhs;
    long, redundant Rhs; Lhs/Rhs attributes placed close together), as the mean
    of three features: length, value, and position score.

    The paper's fourth feature, duplication score (rewarding many duplicate
    values in Lhs/Rhs as a sign the dependency isn't holding by coincidence),
    is left out for now - not relevant to our use case.
    """

    def score(self, dependency, stats):
        scores = (
            self._length_score(dependency, stats),
            self._value_score(stats),
            self._position_score(stats),
            # self._duplication_score(stats),
        )
        return sum(scores) / len(scores)

    def _length_score(self, dependency, stats):
        x, y, r = len(dependency.lhs), len(dependency.rhs), stats.relation_attribute_count
        return 0.5 * (1 / x + 1 / (y * (r - 2)))

    def _value_score(self, stats):
        return 1 / max(1, stats.lhs_max_value_length - 7)

    def _position_score(self, stats):
        between_x = max(stats.lhs_positions) - min(stats.lhs_positions) - (len(stats.lhs_positions) - 1)
        between_y = max(stats.rhs_positions) - min(stats.rhs_positions) - (len(stats.rhs_positions) - 1)
        return 0.5 * (1 / (between_x + 1) + 1 / (between_y + 1))

    # def _duplication_score(self, stats):
    #     duplication_x = stats.lhs_unique_count / stats.lhs_value_count
    #     duplication_y = stats.rhs_unique_count / stats.rhs_value_count
    #     return 0.5 * (2 - duplication_x - duplication_y)


GENUINENESS_SCORES = {
    "probabilistic": ProbabilisticGenuinenessScore,
    "papenbrock": PapenbrockScore,
}


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
