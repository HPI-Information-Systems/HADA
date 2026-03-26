from enum import Enum, auto, unique

from util import container_to_str


@unique
class DependencyType(Enum):
    Functional = auto()
    Order = auto()

    def __str__(self):
        if self == DependencyType.Order:
            return "OD"
        if self == DependencyType.Functional:
            return "FD"
        assert False


class DataDependency:
    def __init__(self, candidate_type, lhs, rhs):
        self.type = candidate_type
        self.lhs = lhs
        self.rhs = rhs

    def __eq__(self, other):
        return self.type == other.type and self.lhs == other.lhs and self.rhs == other.rhs

    def __hash__(self):
        return hash((self.type, self.lhs, self.rhs))

    def to_hint(self, schema):
        lhs_str = ", ".join([f'"{c.column_name}"' for c in self.lhs])
        rhs_str = ""
        tables = [c.table_name for c in self.lhs]
        if isinstance(self.rhs, OdCandidateRhs):
            rhs_str = ", ".join([f'"{c.column_name}"' for c in self.rhs.equalities])
            tables += [c.table_name for c in self.rhs.equalities]
            if self.rhs.inequality:
                rhs_str += f', "{self.rhs.inequality.column_name}"'
                tables.append(self.rhs.inequality.table_name)
        else:
            rhs_str = ", ".join([f'"{c.column_name}"' for c in self.rhs])
            tables += [c.table_name for c in self.rhs]
        assert all([t == tables[0] for t in tables])

        return (
            f"""{{"type": "{str(self.type)}", "table": "{schema}.{tables[0]}", """
            f""""lhs": [{lhs_str}], "rhs": [{rhs_str}]}}"""
        )

    def table(self):
        tables = [column.table_name for column in self.lhs]
        assert len(tables) > 0
        assert all(t == tables[0] for t in tables)
        return tables[0]


class OdCandidateRhs:
    def __init__(self, equalities, inequality):
        self.equalities = frozenset(equalities)
        self.inequality = inequality

    def __eq__(self, other):
        return self.inequality == other.inequality and self.equalities == other.equalities

    def __hash__(self):
        return hash((self.inequality, self.equalities))

    def __str__(self):
        inequality_str = "" if not self.inequality else f" + {self.inequality}"
        return container_to_str(self.equalities) + inequality_str


class OrderDependency(DataDependency):
    def __init__(self, lhs, rhs):
        hashable_lhs = frozenset(lhs) if isinstance(lhs, set) else tuple(lhs)
        hashable_rhs = rhs if isinstance(rhs, OdCandidateRhs) else tuple(rhs)
        super().__init__(DependencyType.Order, hashable_lhs, hashable_rhs)

    def __str__(self):
        rhs_str = str(self.rhs) if isinstance(self.rhs, OdCandidateRhs) else f"{container_to_str(self.rhs)}"
        return f"""{container_to_str(self.lhs)} |=> {rhs_str}"""


class FunctionalDependency(DataDependency):
    def __init__(self, lhs, rhs):
        super().__init__(DependencyType.Functional, frozenset(lhs), frozenset(rhs))

    def __str__(self):
        return f"""{container_to_str(self.lhs)} -> {container_to_str(self.rhs)}"""
