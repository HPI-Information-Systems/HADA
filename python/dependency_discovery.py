import itertools
import re
import time
from collections import defaultdict

from data_dependencies import DependencyType, FunctionalDependency, OdCandidateRhs, OrderDependency
from estimate_genuiness_score import GENUINENESS_SCORES, FdStats, PapenbrockScore
from estimate_genuiness_score import FunctionalDependency as GenuinenessFd
from estimate_genuiness_score import OdGenuinenessEstimator
from estimate_genuiness_score import OrderDependency as GenuinenessOd
from hdbcli import dbapi
from query_plan import Column, Operator, OperatorType, PredicateCondition, Query

# from util import container_to_str, rnd


class DependencyDiscoveryRunner:
    def __init__(self, schemas, fd_rewrite, od_rewrite, od_strategy, od_batch_size, genuineness_strategy="papenbrock"):
        self.valid_fds = defaultdict(set)
        self.valid_ods = defaultdict(set)
        self.invalid_fds = defaultdict(set)
        self.invalid_ods = defaultdict(set)
        # determinant -> {dependent_column: genuineness_score}, populated for every
        # FD candidate that validate_fd() finds invalid.
        self.fd_genuineness = defaultdict(dict)
        # determinant -> {rhs_tuple: genuineness_score}, populated for every
        # OD candidate that validation finds invalid (via add_invalid_od,
        # regardless of the OD validation strategy used).
        self.od_genuineness = defaultdict(dict)
        # Same as fd_genuineness/od_genuineness, but for the dependencies that
        # validation finds to actually hold - scored the same way so valid and
        # invalid dependencies can be compared on the same scale.
        self.valid_fd_genuineness = defaultdict(dict)
        self.valid_od_genuineness = defaultdict(dict)
        self.cursor = None
        self.connection = None
        self.fd_rewrite = fd_rewrite
        self.od_rewrite = od_rewrite
        self.od_validation_strategy = od_strategy
        self.od_batch_size = od_batch_size
        self.genuineness_score = GENUINENESS_SCORES[genuineness_strategy]()
        self.schemas = schemas
        self.catalog = None
        self.table_mapping = None

        self.column_regex = re.compile(r"(?P<table>\w+)\.(?P<column>\w+)")
        self.predicate_regex = re.compile(
            r"(?P<lhs>\S+) (?P<condition>=|<|>|<>|!=|<=|>=|LIKE|NOT\s+LIKE|IS|IN) (?P<rhs>\S+)"
        )
        self.post_join_regex = re.compile("POST JOIN CONDITION:")
        self.conjunction_regex = re.compile(" AND ")
        self.details_regex = re.compile("DETAIL:")

    def setup(self):
        assert self.cursor
        assert self.schemas
        self.cursor.execute("set 'FIELD_NAME_ALIGNMENT' = 'true'")
        # self.cursor.execute("set 'FIELD_NAME_ALIGNMENT' = 'false'")
        self.catalog = self.get_catalog()
        if len(self.schemas) > 1:
            self.table_mapping = self.get_table_mapping(self.catalog)

    def teardown(self):
        self.cursor.close()
        self.connection.close()

    def apply_schema(self):
        if len(self.schemas) == 1:
            self.cursor.execute(f"set schema {self.schemas[0]}")

    def execute_from_file(self, query_file_path):
        with open(query_file_path) as f:
            preparation_queries = f.read().split(";")
        for query in preparation_queries:
            query = query.strip()
            if query and not (len(query.split("\n")) == 1 and query.startswith("--")):
                try:
                    self.cursor.execute(query)
                except Exception:
                    print(f"\nError executing query: {query}")
                    raise

    def drop_schema(self):
        for schema in self.schemas:
            self.cursor.execute(f"drop schema {schema} cascade")

    def run_batch(self, queries):
        assert self.connection and self.cursor

        fd_candidates = defaultdict(set)
        od_candidates = defaultdict(set)
        for query_nr, query in enumerate(queries):
            # print(f'QUERY #{qid}')
            self.parse_query(query, fd_candidates, od_candidates, query_nr)

        print("*" * 40, " CANDIDATES", "*" * 40, sep="\n")
        for lhs, rhs in fd_candidates.items():
            print(FunctionalDependency([lhs], rhs))
        for lhs, rhs in od_candidates.items():
            print(f"""{{ {lhs} }} |=> { " | ".join(str(r) for r in rhs) }""")

        # print("*" * 40, " VALIDATE", "*" * 40, sep="\n")
        for lhs, rhs in fd_candidates.items():
            if len(rhs) == 0:
                continue
            self.validate_fd(lhs, rhs)
        for lhs, rhs in od_candidates.items():
            if len(rhs) == 0:
                continue
            self.validate_od(lhs, rhs)

        print("*" * 40, " VALID", "*" * 40, sep="\n")
        for lhs in sorted(self.valid_fds.keys()):
            rhs = self.valid_fds[lhs]
            if len(rhs) == 0:
                continue
            print(FunctionalDependency([lhs], rhs))
        for lhs in sorted(self.valid_ods.keys()):
            rhs = self.valid_ods[lhs]
            if len(rhs) == 0:
                continue
            for r in sorted(rhs):
                print(OrderDependency([lhs], r))

        print("*" * 40, " QUERIES", "*" * 40, sep="\n")
        for query in queries:
            relevant_dependencies = self.relevant_dependencies_for_query(query)
            if len(relevant_dependencies) == 0:
                continue
            print(self.add_hint(query, relevant_dependencies))
            print("-" * 50)

    def run_single_query(self, query):
        assert self.connection and self.cursor
        fd_candidates = defaultdict(set)
        od_candidates = defaultdict(set)
        self.parse_query(query, fd_candidates, od_candidates)
        for lhs, rhs in fd_candidates.items():
            if len(rhs) == 0:
                continue
            self.validate_fd(lhs, rhs)
        for lhs, rhs in od_candidates.items():
            if len(rhs) == 0:
                continue
            self.validate_od(lhs, rhs)
        relevant_dependencies = self.relevant_dependencies_for_query(query)
        if len(relevant_dependencies) > 0:
            print(self.add_hint(query, relevant_dependencies))

    def get_queries_from_cache(self, schema, catalog):
        queries = []
        for query_list in self.get_cached_statements():
            if self.is_relevant_query(query_list[0], query_list[1], catalog):
                query = Query(query_list[0])
                queries.append(query)
        return queries

    def get_queries_from_file(self, query_file_path):
        with open(query_file_path) as f:
            queries_raw = f.read().split(";")

        queries = []
        for query_str in queries_raw:
            query_str = query_str.strip()
            if query_str:
                queries.append(Query(query_str))
        return queries

    def add_hint(self, query, relevant_dependencies):
        assert len(relevant_dependencies) > 0
        dependency_hints = []
        for d in relevant_dependencies:
            dependency_hints.append(d.to_hint(self.table_schema(d.table())))

        dependencies_str = f"""DEV_DATA_DEPENDENCIES('[{", ".join(dependency_hints)}]')"""
        query_str = ""
        for query_part in query.sql.split(";"):
            hint_splits = re.split(r"WITH\s+HINT\s*\(", query_part, flags=re.IGNORECASE)
            assert len(hint_splits) < 3
            has_hint = len(hint_splits) > 1
            query_str += hint_splits[0]
            if not query_str.endswith("\n"):
                query_str += "\n"
            query_str += (
                f"""WITH HINT(HEX_TABLE_SCAN_SEMI_JOIN, DEV_HEX_EXTENDED_TABLE_SCAN_SEMI_JOIN, {dependencies_str}"""
                f"""{", " + hint_splits[1] if has_hint else ")"}"""
            )
            if not query_str.endswith(";"):
                query_str += ";"
        return query_str

    def find_cached_valid_od(self, candidate_lhs, candidate_rhs):
        assert isinstance(candidate_rhs, OdCandidateRhs)
        required_column_count = len(candidate_rhs.equalities) + (1 if candidate_rhs.inequality else 0)
        for lhs in candidate_lhs:
            for od_columns in self.valid_ods[lhs]:
                # requested dependency might be just the head of the OD's columns, e.g., given OD [1, 2, 3] and
                # requested od {1} + 2 is okay.
                is_match = True
                if len(od_columns) < required_column_count:
                    continue
                for column_id in range(required_column_count):
                    # Inequality always has to be at end, but order of equalities can vary.
                    is_required_equality = od_columns[column_id] in candidate_rhs.equalities
                    is_required_inequality = (
                        candidate_rhs.inequality
                        and column_id == required_column_count - 1
                        and od_columns[column_id] == candidate_rhs.inequality
                    )
                    if not (is_required_equality or is_required_inequality):
                        is_match = False
                        break
                if is_match:
                    return OrderDependency([lhs], od_columns)
        return None

    def relevant_dependencies_for_query(self, query):
        relevant_dependencies = set()
        for candidate in query.dependency_candidates:
            if candidate.type == DependencyType.Functional:
                rhs_columns = list(candidate.rhs)
                lhs_columns = list(candidate.lhs)
                matches = [
                    max([lhs_id if rhs in self.valid_fds[lhs] else -1 for lhs_id, lhs in enumerate(lhs_columns)])
                    for rhs in rhs_columns
                ]
                if any(n < 0 for n in matches):
                    continue
                for lhs_id, lhs in enumerate(lhs_columns):
                    if lhs_id in matches:
                        relevant_dependencies.add(FunctionalDependency([lhs], self.valid_fds[lhs]))
                continue
            if candidate.type == DependencyType.Order:
                od = self.find_cached_valid_od(candidate.lhs, candidate.rhs)
                if od:
                    relevant_dependencies.add(od)
        return relevant_dependencies

    def get_cursor(self, host, port, user, password, autocommit):
        if self.connection and self.cursor:
            self.cursor.close()
            self.connection.close()
        self.connection = dbapi.connect(
            address=host,
            port=port,
            user=user,
            password=password,
            encrypt=False,
            sslValidateCertificate=False,
            autocommit=autocommit,
        )
        self.cursor = self.connection.cursor()

    def get_schemas(self):
        self.cursor.execute(
            "SELECT schema_name FROM schemas WHERE schema_name <> 'SYS' AND schema_name NOT LIKE '_SYS%'"
        )
        return [r[0] for r in self.cursor.fetchall()]

    def get_catalog(self):
        catalog = defaultdict(set)
        schema_list = ", ".join(f"'{schema}'" for schema in self.schemas)
        self.cursor.execute(f"SELECT SCHEMA_NAME, TABLE_NAME FROM TABLES WHERE SCHEMA_NAME IN ({schema_list})")
        for schema, table in self.cursor.fetchall():
            catalog[schema].add(table)
        return catalog

    def get_table_mapping(self, catalog):
        table_mapping = defaultdict(set)
        for schema, tables in catalog.items():
            for table in tables:
                table_mapping[table].add(schema)
        return table_mapping

    def table_schema(self, table_name):
        if len(self.schemas) == 1:
            return self.schemas[0]
        assert self.table_mapping
        schemas = self.table_mapping[table_name]
        assert len(schemas) == 1, f"Could not map table '{table_name}' to schema, candidates are " + ", ".join(
            f"'{s}'" for s in schemas
        )

        # Python does not allow to simply access an element in a set (only `pop()` which removes an element)
        for schema in schemas:
            return schema

    def get_cached_statements(self):
        schema_list = ", ".join(f"'{schema}'" for schema in self.schemas)
        self.cursor.execute(
            "SELECT STATEMENT_STRING, ACCESSED_TABLE_NAMES FROM SYS.M_SQL_PLAN_CACHE WHERE SCHEMA_NAME IN ("
            + schema_list
            + ")"
        )
        return self.cursor.fetchall()

    def is_relevant_query(self, query, accessed_tables, catalog):
        query_upper = query.upper()
        if "SELECT" not in query_upper:
            return False
        for table_names in catalog.values():
            for table_name in table_names:
                if table_name in accessed_tables:
                    return True
        return False

    def parse_od_candidate_predicates(self, predicate_str, left_hand_sides, is_join, left_hand_tables):
        if not self.od_rewrite:
            return None
        details = self.details_regex.search(predicate_str)
        end_pos = len(predicate_str) if not details else details.start()
        predicates = self.predicate_regex.findall(predicate_str.upper(), 0, end_pos)
        pred_count = len(predicates)
        if pred_count == 0 or pred_count - 1 != len(self.conjunction_regex.findall(predicate_str, 0, end_pos)):
            return None

        less_thans = []
        greater_thans = []
        equalities = []

        if not is_join:
            for predicate in predicates:
                left_column = self.column_regex.search(predicate[0])
                right_column = self.column_regex.search(predicate[2])
                if (left_column is None) == (right_column is None):
                    return None
                condition = PredicateCondition.from_str(predicate[1])
                condition = condition if left_column else PredicateCondition.flip(condition)
                column = left_column if left_column else right_column
                column = Column(column.group("table"), column.group("column"))

                if column in left_hand_sides:
                    continue

                if condition == PredicateCondition.Equals:
                    equalities.append(column)
                elif condition == PredicateCondition.Less:
                    less_thans.append(column)
                elif condition == PredicateCondition.Greater:
                    greater_thans.append(column)
                else:
                    return None
        else:
            if pred_count > 1:
                return None
            predicate = predicates[0]
            condition = PredicateCondition.from_str(predicate[1])
            if condition != PredicateCondition.Equals:
                return None
            left_column = self.column_regex.search(predicate[0])
            right_column = self.column_regex.search(predicate[0])
            column = None
            if left_column and left_column.group("table") in left_hand_tables:
                column = left_column
            elif right_column and right_column.group("table") in left_hand_tables:
                column = right_column
            if not column:
                return None
            column = Column(column.group("table"), column.group("column"))
            if column not in left_hand_sides:
                equalities.append(column)

        if len(less_thans) > 1 or len(greater_thans) > 1:
            return None

        if len(less_thans) + len(greater_thans) + len(equalities) == 0:
            return None
        if len(less_thans) > 0 and len(greater_thans) > 0:
            if less_thans[0].column_name != greater_thans[0].column_name:
                return None
        inequality_column = None
        if len(less_thans) > 0:
            inequality_column = less_thans[0]
        elif len(greater_thans) > 0:
            inequality_column = greater_thans[0]
        if not inequality_column and len(equalities) == 1:
            inequality_column = equalities[0]
            equalities = []
        # print(container_to_str(left_hand_sides), OdCandidateRhs(equalities, inequality_column))
        return OrderDependency(left_hand_sides, OdCandidateRhs(equalities, inequality_column))

    def parse_fd_candidate_predicates(self, predicate_str, left_hand_sides, is_join, left_hand_tables):
        if not self.fd_rewrite:
            return None
        details = self.details_regex.search(predicate_str)
        end_pos = len(predicate_str) if not details else details.start()
        predicates = self.predicate_regex.findall(predicate_str.upper(), 0, end_pos)
        pred_count = len(predicates)
        # print(predicates)
        # print(container_to_str(left_hand_sides), predicates)
        if pred_count == 0:
            return None
        columns = set()

        if not is_join:
            for predicate in predicates:
                left_column = self.column_regex.search(predicate[0])
                right_column = self.column_regex.search(predicate[2])

                left_column = Column(left_column.group("table"), left_column.group("column")) if left_column else None
                right_column = (
                    Column(right_column.group("table"), right_column.group("column")) if right_column else None
                )
                left_column = left_column if left_column and left_column.table_name in left_hand_tables else None
                right_column = right_column if right_column and right_column.table_name in left_hand_tables else None

                candidate_columns = [c for c in [left_column, right_column] if c]

                if len(candidate_columns) == 0:
                    return None
                for column in candidate_columns:
                    columns.add(column)
        else:
            if pred_count > 1:
                return None
            predicate = predicates[0]
            condition = PredicateCondition.from_str(predicate[1])
            if condition != PredicateCondition.Equals:
                return None
            left_column = self.column_regex.search(predicate[0])
            right_column = self.column_regex.search(predicate[0])
            column = None
            if left_column and left_column.group("table") in left_hand_tables:
                column = left_column
            elif right_column and right_column.group("table") in left_hand_tables:
                column = right_column
            if not column:
                return None
            column = Column(column.group("table"), column.group("column"))
            if column not in left_hand_sides:
                columns.add(column)

        # if len(columns) > 0:
        #     print(container_to_str(left_hand_sides), predicates)

        return FunctionalDependency(left_hand_sides, columns) if len(columns) > 0 else None

    def find_dependency_candidates(self, join, subtree, query, fd_candidates, od_candidates):
        post_join = self.post_join_regex.search(join.details)
        end_offset = post_join.start() if post_join else len(join.details)
        has_details = self.details_regex.search(join.details, 0, end_offset)
        end_offset = has_details.start() if has_details else end_offset

        predicates = self.predicate_regex.findall(join.details, 0, end_offset)
        # print(join.details, 0, end_offset)
        if len(self.conjunction_regex.findall(join.details, 0, end_offset)) != len(predicates) - 1:
            return
        # assume that key is declared and rewrite to SJ happened --> Candidate on RHS
        left_hand_sides = set()
        for predicate in predicates:
            # print(predicate)
            if predicate[1] != "=":
                return
            column = self.column_regex.search(predicate[2])
            if not column:
                return
            left_hand_sides.add(Column(column.group("table"), column.group("column")))

        left_hand_tables = {col.table_name for col in left_hand_sides}

        index_join = "INDEX" in join.name
        multi_join = len(predicates) > 1
        tssj = join.type == OperatorType.Tssj

        candidates = []
        if index_join and post_join:
            candidates.append(
                self.parse_od_candidate_predicates(join.details[end_offset:], False, left_hand_sides, left_hand_tables)
            )
            if multi_join:
                candidates.append(
                    self.parse_fd_candidate_predicates(
                        join.details[end_offset:], left_hand_sides, False, left_hand_tables
                    )
                )

        # if multi_join:
        #     print(join)

        for line in subtree:
            sub_operator = Operator.from_explain(line)
            is_table = sub_operator.type == OperatorType.Table and sub_operator.table in left_hand_tables
            is_join = sub_operator.is_semi_join()
            if (is_table or is_join) and not index_join:
                candidates.append(
                    self.parse_od_candidate_predicates(sub_operator.details, left_hand_sides, is_join, left_hand_tables)
                )
            if multi_join and (is_join or is_table) and not tssj:
                # print(f"    {str(sub_operator)}")
                candidates.append(
                    self.parse_fd_candidate_predicates(sub_operator.details, left_hand_sides, is_join, left_hand_tables)
                )

        for candidate in candidates:
            if candidate:
                for lhs in candidate.lhs:
                    if candidate.type == DependencyType.Functional:
                        fd_candidates[lhs].update(candidate.rhs)
                    else:
                        od_candidates[lhs].add(candidate.rhs)
                query.dependency_candidates.add(candidate)

    def parse_query(self, query, fd_candidates, od_candidates, query_nr=0):
        # start = time.monotonic()
        # print(query_nr, "prepare", end="", flush=True)
        query_id = f"""dep_check_query_{str(time.monotonic()).replace(".", "")}"""
        # prepare_time = time.monotonic()
        # print("", rnd(prepare_time - start), " explain", end="", flush=True)

        # print("-" * 50, query.sql, sep="\n")
        try:
            self.cursor.execute(f"EXPLAIN PLAN SET STATEMENT_NAME = '{query_id}' FOR {query.sql}")
        except Exception:
            print(f"\nError parsing query {query_nr}")
            raise
        self.cursor.execute(
            "SELECT operator_id, parent_operator_id, operator_name, operator_details, table_name "
            f"FROM SYS.EXPLAIN_PLAN_TABLE WHERE STATEMENT_NAME = '{query_id}' WITH HINT (IGNORE_PLAN_CACHE)"
        )
        # explain_time = time.monotonic()
        # print("", rnd(explain_time - prepare_time), " fetch", end="", flush=True)
        raw_plan = self.cursor.fetchall()
        # fetch_time = time.monotonic()
        # print("", rnd(fetch_time - explain_time), f" parse {len(raw_plan)}", end="", flush=True)
        for idx, line in enumerate(raw_plan):
            # if "JOIN" in line[2]:
            #     print(idx, "    ".join([str(part) for part in line]))
            operator = Operator.from_explain(line)
            if operator.is_semi_join():
                # print(operator.id, operator.name)
                # print(operator)
                self.find_dependency_candidates(operator, raw_plan[idx:], query, fd_candidates, od_candidates)
        # parse_time = time.monotonic()
        # print("", rnd(parse_time - fetch_time), " delete", end="", flush=True)
        self.cursor.execute(
            f"DELETE FROM SYS.EXPLAIN_PLAN_TABLE WHERE STATEMENT_NAME = '{query_id}' WITH HINT (IGNORE_PLAN_CACHE)"
        )
        # end = time.monotonic()
        # print("", rnd(end - parse_time), " sum", rnd(end - start))

        # for line in raw_plan:
        #     # if "JOIN" in line[2]:
        #     print("    ".join([str(part) for part in line]))
        # print()

        # for l, r in fd_candidates.items():
        #     print(f"    {str(l)} -> {container_to_str(r)}")
        # fd_candidates = defaultdict(set)

    def validate_fd(self, determinant, init_dependents):
        dependent_columns = [
            c
            for c in init_dependents
            if c.table_name == determinant.table_name
            and not (c in self.valid_fds[determinant] or c in self.invalid_fds[determinant])
        ]
        if len(dependent_columns) == 0:
            return

        query_template = """SELECT {0}
                              FROM (  SELECT {1}
                                        FROM {2}
                                    GROUP BY {3}
                                   ) internal_agg WITH HINT (IGNORE_PLAN_CACHE);"""

        group_has_null_template = """sum(CASE WHEN {0} IS NULL THEN 1 ELSE 0 END) {0}_has_null"""
        group_dist_cnt_template = "count(distinct {0}) {0}_dst"
        check_template = "max({0}_dst), max({0}_has_null)"
        table = determinant.table_name

        select_list = []
        group_select_list = []

        for column in dependent_columns:
            select_list.append(check_template.format(column.column_name))
            group_select_list.append(group_dist_cnt_template.format(column.column_name))
            group_select_list.append(group_has_null_template.format(column.column_name))

        query = query_template.format(
            ", ".join(select_list),
            ", ".join(group_select_list),
            f"{self.table_schema(table)}.{table}",
            determinant.column_name,
        )

        self.cursor.execute(query)
        res = self.cursor.fetchall()
        assert len(res) == 1
        # print(", ".join(select_list))
        # print(res[0])

        for dependent_idx in range(len(dependent_columns)):
            is_dependent = int(res[0][2 * dependent_idx]) == 1
            no_nulls = int(res[0][2 * dependent_idx + 1]) == 0
            if is_dependent and no_nulls:
                dependent_column = dependent_columns[dependent_idx]
                self.valid_fds[determinant].add(dependent_column)
                self.valid_fd_genuineness[determinant][dependent_column] = self.compute_fd_genuineness(
                    determinant, dependent_column
                )
            else:
                dependent_column = dependent_columns[dependent_idx]
                self.invalid_fds[determinant].add(dependent_column)
                self.fd_genuineness[determinant][dependent_column] = self.compute_fd_genuineness(
                    determinant, dependent_column
                )

    def compute_fd_genuineness(self, determinant, dependent_column):
        """Computes the genuineness score of the invalid FD candidate
        `determinant -> dependent_column` against the real data, using whichever
        GenuinenessScore strategy was configured (see GENUINENESS_SCORES in
        estimate_genuiness_score.py): the default "papenbrock" strategy instead scores how plausible a real FD of this
        shape looks (Papenbrock et al. 2017, Section 7.2 "Violating FD selection"); 
        the alternative "probabilistic" strategy (Algorithm 1, "Estimate Genuineness Score") computes the probability that
        the FD would still hold if every row whose dependent value disagrees with
        its determinant's majority redrew that value from the empirical
        distribution observed for that determinant value.
        """
        assert determinant.table_name == dependent_column.table_name
        table = determinant.table_name
        lhs_col = determinant.column_name
        rhs_col = dependent_column.column_name

        fd = GenuinenessFd(lhs=(lhs_col,), rhs=(rhs_col,))

        if isinstance(self.genuineness_score, PapenbrockScore):
            stats = self.compute_papenbrock_stats(table, (lhs_col,), (rhs_col,))
        else:
            stats = self.compute_fd_probabilistic_stats(table, lhs_col, rhs_col)

        return self.genuineness_score.score(fd, stats)

    def compute_fd_probabilistic_stats(self, table, lhs_col, rhs_col):
        query = f"""
        WITH grp AS (
            SELECT {lhs_col}, {rhs_col}, COUNT(*) AS cnt
            FROM {self.table_schema(table)}.{table}
            WHERE {lhs_col} IS NOT NULL AND {rhs_col} IS NOT NULL
            GROUP BY {lhs_col}, {rhs_col}
        ),
        ambiguous AS (
            SELECT {lhs_col}
            FROM grp
            GROUP BY {lhs_col}
            HAVING COUNT(*) > 1
        )
        SELECT grp.{lhs_col}, grp.{rhs_col}, grp.cnt
        FROM grp
        JOIN ambiguous ON grp.{lhs_col} = ambiguous.{lhs_col}
        """
        self.cursor.execute(query)
        rows = self.cursor.fetchall()

        rhs_counts_by_lhs = defaultdict(dict)
        for lhs_value, rhs_value, cnt in rows:
            rhs_counts_by_lhs[(lhs_value,)][(rhs_value,)] = cnt

        return FdStats(rhs_counts_by_lhs=rhs_counts_by_lhs)

    def compute_papenbrock_stats(self, table, lhs_cols, rhs_cols):
        """Gathers the stats needed by PapenbrockScore (length/value/position
        features) for a violating dependency `lhs_cols -> rhs_cols`, an FD or an
        OD - the features apply unchanged to both.
        """
        schema = self.table_schema(table)

        self.cursor.execute(
            f"SELECT COLUMN_NAME, POSITION FROM COLUMNS WHERE SCHEMA_NAME = '{schema}' AND TABLE_NAME = '{table}'"
        )
        positions = {column_name: position for column_name, position in self.cursor.fetchall()}

        lhs_max_value_length = 0
        for lhs_col in lhs_cols:
            self.cursor.execute(
                f"SELECT MAX(LENGTH(TO_NVARCHAR({lhs_col}))) FROM {schema}.{table} WHERE {lhs_col} IS NOT NULL"
            )
            (max_length,) = self.cursor.fetchone()
            lhs_max_value_length = max(lhs_max_value_length, max_length or 0)

        return FdStats(
            relation_attribute_count=len(positions),
            lhs_positions=tuple(positions[c] for c in lhs_cols),
            rhs_positions=tuple(positions[c] for c in rhs_cols),
            lhs_max_value_length=lhs_max_value_length,
        )

    def compute_od_genuineness(self, determinant, rhs_columns):
        """Computes the genuineness score of the invalid OD candidate
        `determinant |=> rhs_columns` against the real data: the probability that
        the OD would still hold if every row whose rhs tuple disagrees with its
        determinant's majority redrew that tuple from the empirical distribution
        observed for that determinant value.

        An OD combines two constraints: rows sharing a determinant value must
        agree on the SAME rhs tuple (an embedded FD, handled per group exactly
        like compute_fd_genuineness), and the agreed-upon tuples must be
        lexicographically non-decreasing across determinant values in sorted
        order (handled by OdGenuinenessEstimator's chained DP - see its
        docstring for why adjacent-group checks suffice).

        With the "papenbrock" strategy, neither of these matters: the OD is
        instead scored on how plausible a real dependency of this shape looks
        (Papenbrock et al. 2017, Section 7.2 "Violating FD selection"), the same
        way as for FDs.
        """
        assert determinant.table_name == rhs_columns[0].table_name
        table = determinant.table_name
        lhs_col = determinant.column_name
        rhs_cols = [c.column_name for c in rhs_columns]

        if isinstance(self.genuineness_score, PapenbrockScore):
            od = GenuinenessOd(lhs=(lhs_col,), rhs=tuple(rhs_cols))
            stats = self.compute_papenbrock_stats(table, (lhs_col,), tuple(rhs_cols))
            return self.genuineness_score.score(od, stats)

        not_null = " AND ".join(f"{c} IS NOT NULL" for c in [lhs_col] + rhs_cols)
        query = f"""
        SELECT {lhs_col}, {", ".join(rhs_cols)}, COUNT(*) AS cnt
        FROM {self.table_schema(table)}.{table}
        WHERE {not_null}
        GROUP BY {lhs_col}, {", ".join(rhs_cols)}
        """
        self.cursor.execute(query)
        rows = self.cursor.fetchall()

        rhs_counts_by_lhs = defaultdict(dict)
        for row in rows:
            lhs_value = row[0]
            rhs_value = tuple(row[1:-1])
            cnt = row[-1]
            rhs_counts_by_lhs[lhs_value][rhs_value] = cnt

        groups = []
        for lhs_value in sorted(rhs_counts_by_lhs):
            rhs_counts = rhs_counts_by_lhs[lhs_value]
            total = sum(rhs_counts.values())
            # Probability that every one of the `total` rows sharing this lhs
            # value independently redraws to the SAME rhs tuple v: each row
            # draws v with probability cnt_v / total, so all of them doing so
            # is (cnt_v / total) ** total - mirrors compute_fd_genuineness.
            distribution = {v: (cnt / total) ** total for v, cnt in rhs_counts.items()}
            groups.append((lhs_value, distribution))

        od = GenuinenessOd(lhs=(lhs_col,), rhs=tuple(rhs_cols))
        return OdGenuinenessEstimator(od).estimate(groups)

    def validate_od(self, determinant, right_hand_sides):
        # As everything depends on one single column, we only have to sort and retrieve the result set once and run
        # different checks on this set.
        unique_query_columns = set()
        common_table = determinant.table_name
        for rhs in right_hand_sides:
            assert isinstance(rhs, OdCandidateRhs)
            if self.find_cached_valid_od([determinant], rhs):
                continue
            unique_query_columns |= rhs.equalities | {rhs.inequality}
        unique_query_columns = [c for c in unique_query_columns if c and c.table_name == common_table]
        if len(unique_query_columns) == 0:
            return

        od_candidates = set()
        for candidate in right_hand_sides:
            # Inequality must always be at the end, the equalities can be arbitraily shuffled.
            for permutation in itertools.permutations(list(candidate.equalities)):
                # We do not need to do something if this combination was tested and rejected before. The OD cannot hold
                # if we invalidated an FD candidate for these columns.
                candidate_columns = list(permutation)
                if candidate.inequality:
                    candidate_columns.append(candidate.inequality)
                od_is_invalid = tuple(candidate_columns) in self.invalid_ods[determinant]
                fd_is_invalid = any(c in self.invalid_fds[determinant] for c in candidate_columns)
                if od_is_invalid or fd_is_invalid:
                    continue
                od_candidates.add(tuple(candidate_columns))
        if len(od_candidates) == 0:
            return
        od_candidates = [list(c) for c in od_candidates]

        # print(f"""check OD {str(determinant)} |=> {" | ".join(str(r) for r in right_hand_sides)}""")
        # print(len(od_candidates), "permutations")
        # start = time.monotonic()
        if self.od_validation_strategy == "checkers":
            self.validate_od_with_checkers(common_table, determinant, od_candidates, unique_query_columns)
        elif self.od_validation_strategy == "query":
            self.validate_od_with_query(common_table, determinant, od_candidates, unique_query_columns)
        elif self.od_validation_strategy == "query_rank":
            self.validate_od_with_query_rank(common_table, determinant, od_candidates)
        else:
            raise NotImplementedError(f"No such OD validation strategy '{self.od_validation_strategy}'")
        # end = time.monotonic()
        # print(f"checked in {rnd(end - start)}")

    def validate_od_with_query(self, common_table, determinant, permutations, unique_query_columns):
        query_template = """SELECT {0}
                              FROM (SELECT {1}
                                      FROM (SELECT {2}
                                              FROM {3}
                                           ) internal_ger_prev
                                   ) internal_compare WITH HINT(IGNORE_PLAN_CACHE);"""
        top_aggregations = []
        cases = []
        selects = []
        for column in unique_query_columns:
            selects.append(column.column_name)
            selects.append(
                f"lag({column.column_name}) OVER (ORDER BY {determinant.column_name}) AS prev_{column.column_name}"
            )

        for permutation in permutations:
            identifier = "valid_" + "_".join(column.column_name for column in permutation)
            top_aggregations.append(f"min({identifier})")
            internal_cases = []
            for column in permutation:
                internal_cases.append(f"{column.column_name} IS NULL")
            for column_id, column in enumerate(permutation):
                previous_concats = []
                for prev_column in permutation[:column_id]:
                    previous_concats.append(f"prev_{prev_column.column_name} = {prev_column.column_name}")
                previous_concat = " AND ".join(previous_concats)
                column_comparison = f"{column.column_name} < prev_{column.column_name}"
                internal_cases.append(f"""({previous_concat}{" AND " if column_id > 0 else ""}{column_comparison})""")
            cases.append(f"""CASE WHEN {" OR ".join(internal_cases)} THEN -1 ELSE 0 END AS {identifier}""")

        query = query_template.format(
            ", ".join(top_aggregations),
            ", ".join(cases),
            ", ".join(selects),
            f"{self.table_schema(common_table)}.{common_table}",
        )
        # print(query)
        self.cursor.execute(query)
        result = self.cursor.fetchall()
        assert len(result) == 1 and len(result[0]) == len(permutations)
        for result_id, marker in enumerate(result[0]):
            assert marker == 0 or marker == -1
            if marker == 0:
                self.add_valid_od(determinant, tuple(permutations[result_id]))
            else:
                self.add_invalid_od(determinant, tuple(permutations[result_id]))

    def validate_od_with_query_rank(self, common_table, determinant, permutations):
        query_template = """SELECT {0}
                              FROM (SELECT {1}
                                      FROM (SELECT {2}, {3}
                                              FROM {4}
                                           ) internal_rank
                                   ) internal_rank_offsets WITH HINT(IGNORE_PLAN_CACHE);"""
        top_aggregations = []
        rank_offsets = []
        ranks = []

        for permutation in permutations:
            identifier = "_".join(c.column_name for c in permutation)
            top_aggregations.append(f"min(valid_{identifier})")
            rank_offsets.append(
                "rnk_{0} - lag(rnk_{0}, 1, 0) OVER (ORDER BY {1}) AS valid_{0}".format(
                    identifier, determinant.column_name
                )
            )
            ranks.append(
                f"""rank() OVER (ORDER BY {", ".join(c.column_name for c in permutation)}) AS rnk_{identifier}"""
            )

        query = query_template.format(
            ", ".join(top_aggregations),
            ", ".join(rank_offsets),
            determinant.column_name,
            ", ".join(ranks),
            f"{self.table_schema(common_table)}.{common_table}",
        )

        self.cursor.execute(query)
        result = self.cursor.fetchall()
        assert len(result) == 1 and len(result[0]) == len(permutations)
        for result_id, marker in enumerate(result[0]):
            assert isinstance(marker, int)
            if marker >= 0:
                self.add_valid_od(determinant, tuple(permutations[result_id]))
            else:
                self.add_invalid_od(determinant, tuple(permutations[result_id]))

    def validate_od_with_checkers(self, common_table, determinant, permutations, unique_query_columns):
        query_template = "SELECT {0} FROM {1} ORDER BY {2} WITH HINT(IGNORE_PLAN_CACHE)"
        query = query_template.format(
            ", ".join(c.column_name for c in unique_query_columns),
            f"{self.table_schema(common_table)}.{common_table}",
            determinant.column_name,
        )

        checkers = [OdChecker(permutation, unique_query_columns) for permutation in permutations]

        # Run on a subset of data to avoid sorting large tables for invalid ODs. Removed for now: Bottleneck was
        # comparing many tuples for VALID candidates in Python.
        #
        # query = query_template.format(
        #     ", ".join(c.column_name for c in unique_query_columns),
        #     f" (SELECT * FROM {self.table_schema(common_table)}.{common_table} LIMIT 100) sample",
        #     determinant.column_name,
        # )
        # self.run_od_checkers(query, checkers)
        # valid_checkers = []
        # for checker in checkers:
        #     if checker.is_valid:
        #         checker.reset()
        #         valid_checkers.append(checker)
        #     else:
        #         self.invalid_ods[determinant].add(checker.to_od())
        # if len(valid_checkers) == 0:
        #     return
        valid_checkers = checkers

        query = query_template.format(
            ", ".join(c.column_name for c in unique_query_columns),
            f"{self.table_schema(common_table)}.{common_table}",
            determinant.column_name,
        )
        self.run_od_checkers(query, valid_checkers)

        valid_count = 0
        for checker in valid_checkers:
            if checker.is_valid:
                self.add_valid_od(determinant, checker.to_od())
                valid_count += 1
            else:
                self.add_invalid_od(determinant, checker.to_od())

    def run_od_checkers(self, query, checkers):
        fetch_next = True
        # start = time.monotonic()
        self.cursor.execute(query)
        # execute = time.monotonic()
        # fetch = 0
        # process = 0
        # Do not fetch the entire result set, but only if there are still valid candidates.
        while fetch_next:
            # fetch_start = time.monotonic()
            batch = self.cursor.fetchmany(self.od_batch_size)
            # fetch_end = time.monotonic()
            fetch_next = len(batch) == self.od_batch_size
            for row in batch:
                if not fetch_next:
                    break
                fetch_next = fetch_next and any([c.check(row) for c in checkers])
            # process_end = time.monotonic()
            # fetch += fetch_end - fetch_start
            # process += process_end - fetch_end
        # end = time.monotonic()
        # print(f"execute {rnd(execute - start)}  fetch {rnd(fetch)}  process {rnd(process)}  sum {rnd(end - start)}")

    def add_valid_od(self, lhs, rhs):
        # Account for minimality of ODs: [a] |=> [b, c] can be derived from [a] |=> [b, c, d]. If the second one is
        # already stored, we do not have to store the first one. If the first one is already stored, we can add the
        # remaining column(s) of the second one.

        for known_rhs in self.valid_ods[lhs]:
            if len(rhs) <= len(known_rhs):
                if rhs == known_rhs[: len(rhs)]:
                    # OD (or refining OD) already known.
                    return
                continue

            if rhs[: len(known_rhs)] == known_rhs:
                # New OD refines already known OD. We cannot simply transform the existing one (stored as a tuple for
                # hashability in the RHS set), so we have to delete the existing RHS and add the new one.
                self.valid_ods[lhs].remove(known_rhs)
                self.valid_od_genuineness[lhs].pop(known_rhs, None)
                break

        self.valid_ods[lhs].add(rhs)
        # Score the valid OD the same way invalid ones are scored (see
        # add_invalid_od), so both can be compared on the same scale.
        self.valid_od_genuineness[lhs][rhs] = self.compute_od_genuineness(lhs, list(rhs))

    def add_invalid_od(self, lhs, rhs):
        # Single registration point for invalid ODs (mirrors add_valid_od), used
        # by all OD validation strategies so the genuineness score is computed
        # regardless of which strategy rejected the OD.
        self.invalid_ods[lhs].add(rhs)
        self.od_genuineness[lhs][rhs] = self.compute_od_genuineness(lhs, list(rhs))


class OdChecker:
    def __init__(self, check_columns, result_columns):
        self.columns = check_columns
        self.column_ids = [result_columns.index(c) for c in self.columns]
        self.values = None
        self.is_init = False
        self.is_valid = True

    def check(self, result_row):
        if not self.is_valid:
            return False

        next_values = [result_row[idx] for idx in self.column_ids]
        if any(v is None for v in next_values):
            self.is_valid = False
            return False

        if not self.is_init:
            self.values = next_values
            self.is_init = True
            return True

        if self.values > next_values:
            self.is_valid = False
            return False

        self.values = next_values

        return True

    def reset(self):
        self.values = None
        self.is_init = False
        self.is_valid = True

    def to_od(self):
        return tuple(self.columns)
