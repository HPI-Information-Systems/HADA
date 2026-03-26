from enum import Enum, auto, unique


class Query:
    def __init__(self, sql):
        self.sql = sql
        self.dependency_candidates = set()


@unique
class PredicateCondition(Enum):
    Equals = auto()
    Less = auto()
    Greater = auto()
    Between = auto()
    NotEquals = auto()
    Other = auto()

    @staticmethod
    def from_str(condition):
        if condition == "=":
            return PredicateCondition.Equals
        if condition in ["!=", "<>"]:
            return PredicateCondition.NotEquals
        if condition in ["<", "<="]:
            return PredicateCondition.Less
        if condition in [">", ">="]:
            return PredicateCondition.Greater
        return PredicateCondition.Other

    @staticmethod
    def flip(condition):
        if condition == PredicateCondition.Less:
            return PredicateCondition.Greater
        if condition == PredicateCondition.Greater:
            return PredicateCondition.Less
        return condition


@unique
class OperatorType(Enum):
    Other = auto()
    Join = auto()
    Table = auto()
    View = auto()
    UnionAll = auto()
    Tssj = auto()

    @staticmethod
    def from_operator_name(name):
        if "TABLE SCAN SEMI JOIN" in name:
            return OperatorType.Tssj
        if "JOIN" in name:
            return OperatorType.Join
        if "TABLE SCAN" in name:
            return OperatorType.Table
        return OperatorType.Other


class Operator:
    def __init__(self, op_id, parent_id, name, details, table):
        self.id = op_id
        self.parent_id = parent_id
        self.name = name
        self.details = details
        self.table = table
        self.type = OperatorType.from_operator_name(self.name)

    @staticmethod
    def from_explain(line):
        assert len(line) == 5
        return Operator(line[0], line[1], line[2].strip(), line[3], line[4])

    def __str__(self):
        return f"#{self.id} {self.type} {self.name} {self.details}"

    def is_semi_join(self):
        return self.type == OperatorType.Tssj or (
            self.type == OperatorType.Join and "SEMI" in self.name and "ANTI" not in self.name
        )


class Column:
    def __init__(self, table_name, column_name):
        self.table_name = table_name
        self.column_name = column_name

    def __eq__(self, other):
        return self.table_name == other.table_name and self.column_name == other.column_name

    def __lt__(self, other):
        return (self.table_name, self.column_name) < (other.table_name, other.column_name)

    def __hash__(self):
        return hash((self.table_name, self.column_name))

    def __str__(self):
        return f"{self.table_name}.{self.column_name}"
