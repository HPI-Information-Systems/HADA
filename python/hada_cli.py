#!/usr/bin/env python3

import argparse
import json
import sys

from dependency_discovery import DependencyDiscoveryRunner


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--schema", "-s", type=str, default=["SYSTEM"], help="Used schema(s). Default: SYSTEM", nargs="+"
    )
    parser.add_argument(
        "--fd-rewrite",
        action="store_true",
        help="Generate and validate candidates for the FD-based multi-predicate TSSJ for genuine semi-joins (default)",
    )
    parser.add_argument(
        "--no-fd-rewrite",
        dest="fd_rewrite",
        action="store_false",
        help="Do not generate and validate candidates for the FD-based multi-predicate TSSJ for genuine semi-joins",
    )
    parser.set_defaults(fd_rewrite=True)
    parser.add_argument(
        "--od-rewrite",
        action="store_true",
        help="Generate and validate candidates for the OD-based range-predicated TSSJ",
    )
    parser.add_argument(
        "--no-od-rewrite",
        dest="od_rewrite",
        action="store_false",
        help="Do not generate and validate candidates for the OD-based range-predicated TSSJ (default)",
    )
    parser.add_argument(
        "--queries",
        "-q",
        type=str,
        default=None,
        help="File with SQL queries. If not provided, use the SQL plan cache for the provided schema",
    )
    parser.add_argument("--import-queries", "-i", type=str, default=None, help="File with SQL queries to load the data")
    parser.add_argument(
        "--preparation-queries",
        "-p",
        type=str,
        default=None,
        help="File with SQL queries to prepare the workspace (e.g., to set session parameters)",
    )
    parser.add_argument(
        "--cleanup-queries",
        "-c",
        type=str,
        default=None,
        help="File with SQL queries to cleanup the workspace (e.g., to remove temporary tables)",
    )
    parser.add_argument("--drop-schema", action="store_true", help="Drop the used schema after finishing")
    parser.add_argument(
        "--od-strategy",
        type=str,
        default="query",
        choices=["query", "checkers", "query_rank"],
        help="OD validation strategy. Default: query",
    )
    parser.add_argument(
        "--od-batch-size",
        type=int,
        default=1000,
        help="Batch size to retrieve data while validating order dependencies using the 'checkers' strategy. "
        "Smaller values benefit invalid candidates, larger values benefit valid candidates. Default: 1000",
    )
    parser.add_argument(
        "--db-connection-file",
        type=str,
        default="./database_connection.json",
        help="JSON file containing the DB connection information. For an example, see "
        "'resources/sample_database_connection.json'. Default: ./database_connection.json",
    )
    return parser.parse_args()


def load_connection_info(connection_file):
    with open(connection_file, "r") as f:
        return json.load(f)


if __name__ == "__main__":
    args = parse_args()
    if not (args.od_rewrite or args.fd_rewrite):
        print("Error: No target rewrite selected")
        sys.exit(1)

    connection_info = load_connection_info(args.db_connection_file)
    runner = DependencyDiscoveryRunner(
        args.schema, args.fd_rewrite, args.od_rewrite, args.od_strategy, args.od_batch_size
    )
    runner.get_cursor(
        connection_info["host"],
        connection_info["port"],
        connection_info["db_user"],
        connection_info["db_user_password"],
        connection_info["autocommit"],
    )
    runner.setup()
    if args.import_queries:
        print("Importing data ...", end="", flush=True)
        runner.execute_from_file(args.import_statements)
        print(" done")
    runner.apply_schema()
    if args.preparation_queries:
        runner.execute_from_file(args.preparation_queries)
    if args.queries:
        queries = runner.get_queries_from_file(args.queries)
    else:
        queries = runner.get_queries_from_cache()
    runner.run_batch(queries)
    if args.cleanup_queries:
        runner.execute_from_file(args.cleanup_queries)
    if args.drop_schema:
        runner.drop_schema()
    runner.teardown()
