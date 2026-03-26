from pathlib import Path
import re
import time
import copy
import random
import base64
from collections import defaultdict
import pandas as pd
import altair as alt
import streamlit as st
st.set_page_config(page_title="HaDA", layout="wide")
from dependency_discovery import DependencyDiscoveryRunner
from streamlit_ace import st_ace


# Extended color palette for dependencies 
DEPENDENCY_COLORS = [
    "#FF6B6B",  # Red
    "#4ECDC4",  # Teal
    "#45B7D1",  # Blue
    "#96CEB4",  # Green
    "#FFEAA7",  # Yellow
    "#DDA0DD",  # Plum
    "#FF8C00",  # Dark Orange
    "#20B2AA",  # Light Sea Green
    "#BA55D3",  # Medium Orchid
    "#F0E68C",  # Khaki
    "#CD5C5C",  # Indian Red
    "#6495ED",  # Cornflower Blue
]

# Colors for execution bar chart
BAR_COLORS = {
    'Original': '#FF6B6B',    # Red for original query
    'Rewritten': '#4CAF50',   # Green for rewritten query
}


class UserInputState:
    def __init__(self):
        self.current = ""
        self.previous = ""

    def advance(self):
        self.previous = self.current

    def has_changed(self):
        return not emptystring(self.current) and self.current != self.previous


def emptystring(string):
    if not string or string.isspace():
        return True
    return False


def reset_dependency_state():
    """Reset all dependency-related state when query changes."""
    st.session_state.selected_dependencies_set = set()
    st.session_state.selected_dep_colors = {}
    st.session_state.rewritten_query = ""
    st.session_state.original_plan = None
    st.session_state.rewritten_plan = None
    st.session_state.validated_candidates = set()
    st.session_state.manually_added_deps = set()
    if hasattr(st.session_state, 'runner'):
        st.session_state.runner.valid_fds.clear()
        st.session_state.runner.valid_ods.clear()


def remove_duplicate_hints(query_str):
    """Remove duplicate DEV_DATA_DEPENDENCIES entries from query hint."""
    hint_pattern = r"DEV_DATA_DEPENDENCIES\s*\(\s*'\s*\[(.*?)\]\s*'\s*\)"
    matches = re.findall(hint_pattern, query_str, re.DOTALL | re.IGNORECASE)
    
    if not matches:
        return query_str
    
    all_entries = []
    seen_entries = set()
    
    for match in matches:
        entry_pattern = r'\{[^}]+\}'
        entries = re.findall(entry_pattern, match)
        for entry in entries:
            normalized = re.sub(r'\s+', ' ', entry.strip())
            if normalized not in seen_entries:
                seen_entries.add(normalized)
                all_entries.append(entry.strip())
    
    if not all_entries:
        return query_str
    
    cleaned_query = re.sub(hint_pattern, '', query_str, flags=re.DOTALL | re.IGNORECASE)
    cleaned_query = re.sub(r',\s*,', ',', cleaned_query)
    cleaned_query = re.sub(r'\(\s*,', '(', cleaned_query)
    cleaned_query = re.sub(r',\s*\)', ')', cleaned_query)
    
    consolidated_hint = f"DEV_DATA_DEPENDENCIES('[{', '.join(all_entries)}]')"
    hint_clause_pattern = r'WITH\s+HINT\s*\(([^)]*)\)'
    
    def replace_hint(match):
        existing = match.group(1).strip()
        if existing:
            return f"WITH HINT({existing}, {consolidated_hint})"
        return f"WITH HINT({consolidated_hint})"
    
    if re.search(hint_clause_pattern, cleaned_query, re.IGNORECASE):
        result = re.sub(hint_clause_pattern, replace_hint, cleaned_query, flags=re.IGNORECASE)
    else:
        result = cleaned_query.rstrip().rstrip(';')
        result += f"\nWITH HINT({consolidated_hint});"
    
    return result


def format_dependency_hint(dtype, lhs, rhs, color_idx=None):
    try:
        lhs_cols = [f'"{lhs.column_name}"']
        table = lhs.table_name
        if isinstance(rhs, (set, list, tuple, frozenset)):
            rhs_cols = [f'"{c.column_name}"' for c in rhs]
        else:
            rhs_cols = [f'"{rhs.column_name}"'] if hasattr(rhs, 'column_name') else []
        table_full = f"SYSTEM.{table}"
        return (
            f'{{"type": "{dtype}", "table": "{table_full}", '
            f'"lhs": [{", ".join(lhs_cols)}], "rhs": [{", ".join(rhs_cols)}]}}'
        )
    except Exception:
        return '{"type": "FD", "table": "SYSTEM.DEMO", "lhs": [], "rhs": []}'


def apply_rewrite_to_query(base_query, hint_json_list):
    hint_payload = ", ".join(hint_json_list) if isinstance(hint_json_list, (list, tuple)) else str(hint_json_list)
    rewritten = base_query + f"\nWITH HINT(DEV_DATA_DEPENDENCIES('[{hint_payload}]'))"
    return remove_duplicate_hints(rewritten)


def parse_explain_plan(runner, query_sql):
    query_id = f"""dep_check_query_{str(time.monotonic()).replace(".", "")}"""
    
    try:
        runner.cursor.execute(f"EXPLAIN PLAN SET STATEMENT_NAME = '{query_id}' FOR {query_sql}")
        runner.cursor.execute(
            "SELECT operator_id, parent_operator_id, operator_name, operator_details, table_name "
            f"FROM SYS.EXPLAIN_PLAN_TABLE WHERE STATEMENT_NAME = '{query_id}' WITH HINT (IGNORE_PLAN_CACHE)"
        )
        raw_plan = runner.cursor.fetchall()
        runner.cursor.execute(
            f"DELETE FROM SYS.EXPLAIN_PLAN_TABLE WHERE STATEMENT_NAME = '{query_id}' WITH HINT (IGNORE_PLAN_CACHE)"
        )
        
        plan_nodes = []
        for line in raw_plan:
            node = {
                'id': line[0],
                'parent_id': line[1],
                'name': line[2].strip(),
                'details': line[3],
                'table': line[4],
                'children': []
            }
            plan_nodes.append(node)
        
        node_map = {node['id']: node for node in plan_nodes}
        root_nodes = []
        for node in plan_nodes:
            if node['parent_id'] is None or node['parent_id'] not in node_map:
                root_nodes.append(node)
            else:
                parent = node_map[node['parent_id']]
                parent['children'].append(node)
        
        return {'nodes': plan_nodes, 'root': root_nodes}
    except Exception as e:
        return {'error': str(e), 'nodes': [], 'root': []}


def generate_graphviz_dot(nodes, title="Query Plan", highlight_nodes=None, show_details=True):
    """Generate a simple Graphviz DOT for a single query plan.
    All nodes are gray - no color coding for TSSJ or optimizations."""
    highlight_nodes = highlight_nodes or set()
    
    dot_lines = [
        'digraph QueryPlan {',
        '  rankdir=BT;',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10, margin="0.2,0.1"];',
        '  edge [arrowsize=0.7];',
        '  graph [ranksep=0.8, nodesep=0.5];',
        f'  labelloc="t";',
        f'  label="{title}";',
        ''
    ]
    
    for node in nodes:
        is_highlighted = node['id'] in highlight_nodes
        
        # Simple gray for all nodes 
        if is_highlighted:
            fillcolor = "#FFD700"  # Yellow for highlighted
            style = "rounded,filled,bold"
        else:
            fillcolor = "#E8E8E8"  # Gray for all nodes
            style = "rounded,filled"
        
        # Build label with name, table, and details
        label = node['name']
        if node['table']:
            label += f"\\n({node['table']})"
        # Show details if available (this shows the predicate/condition)
        if show_details and node.get('details'):
            details = node['details']
            # Truncate long details
            if len(details) > 50:
                details = details[:47] + "..."
            label += f"\\n{details}"
        label = label.replace('"', '\\"')
        
        dot_lines.append(f'  n{node["id"]} [label="{label}", fillcolor="{fillcolor}", style="{style}"];')
    
    # Edges: child -> parent (data flows upward: TABLE SCAN -> TSSJ -> PROJECT)
    for node in nodes:
        if node['parent_id'] is not None:
            dot_lines.append(f'  n{node["id"]} -> n{node["parent_id"]};')
    
    dot_lines.append('}')
    return '\n'.join(dot_lines)


def wrap_text(text, max_chars=45):
    """Wrap text into multiple lines for better display in graphviz nodes."""
    if not text or len(text) <= max_chars:
        return text
    
    # Split on spaces, keeping operators together
    words = text.split()
    lines = []
    current_line = []
    current_length = 0
    
    for word in words:
        if current_length + len(word) + 1 > max_chars and current_line:
            lines.append(' '.join(current_line))
            current_line = [word]
            current_length = len(word)
        else:
            current_line.append(word)
            current_length += len(word) + 1
    
    if current_line:
        lines.append(' '.join(current_line))
    
    return '\\n'.join(lines)


def generate_comparison_graphviz(original_nodes, rewritten_nodes):
    """Generate side-by-side comparison of query plans with correct binary tree structure."""
    orig_by_id = {node['id']: node for node in original_nodes}
    
    dot_lines = [
        'digraph Comparison {',
        '  rankdir=BT;',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10, margin="0.15,0.08"];',
        '  edge [arrowsize=0.5];',
        '  graph [ranksep=0.35, nodesep=0.2, compound=true];',
        '',
        '  subgraph cluster_original {',
        '    label="Original Plan";',
        '    fontsize=10;',
        '    style=dashed;',
        '    color=gray;',
    ]
    
    for node in original_nodes:
        fillcolor = "#E8E8E8"
        label_parts = [node['name']]
        if node['table']:
            label_parts.append(f"[{node['table']}]")
        if node.get('details'):
            label_parts.append(wrap_text(node['details'], 40))
        label = '\\n'.join(label_parts).replace('"', '\\"')
        dot_lines.append(f'    orig_{node["id"]} [label="{label}", fillcolor="{fillcolor}"];')
    
    # Generate edges: child -> parent (data flows upward: TABLE SCAN -> TSSJ -> PROJECT)
    for node in original_nodes:
        if node['parent_id'] is not None:
            dot_lines.append(f'    orig_{node["id"]} -> orig_{node["parent_id"]};')
    
    dot_lines.append('  }')
    dot_lines.append('')
    dot_lines.append('  subgraph cluster_rewritten {')
    dot_lines.append('    label="Rewritten Plan";')
    dot_lines.append('    fontsize=10;')
    dot_lines.append('    style=dashed;')
    dot_lines.append('    color=gray;')
    
    for node in rewritten_nodes:
        orig_node = orig_by_id.get(node['id'])
        is_optimized = False
        if orig_node:
            if orig_node['name'] != node['name']:
                is_optimized = True
            elif orig_node.get('details') != node.get('details'):
                if 'BETWEEN' in (node.get('details') or ''):
                    is_optimized = True
        else:
            is_optimized = True
        
        if is_optimized:
            fillcolor = "#90EE90"
            penwidth = "2"
        else:
            fillcolor = "#E8E8E8"
            penwidth = "1"
        
        label_parts = [node['name']]
        if node['table']:
            label_parts.append(f"[{node['table']}]")
        if node.get('details'):
            label_parts.append(wrap_text(node['details'], 40))
        label = '\\n'.join(label_parts).replace('"', '\\"')
        dot_lines.append(f'    rewr_{node["id"]} [label="{label}", fillcolor="{fillcolor}", penwidth="{penwidth}"];')
    
    for node in rewritten_nodes:
        if node['parent_id'] is not None:
            dot_lines.append(f'    rewr_{node["id"]} -> rewr_{node["parent_id"]};')
    
    dot_lines.append('  }')
    dot_lines.append('}')
    
    return '\n'.join(dot_lines)


def get_cached_queries(runner, schema=None):
    try:
        schema_filter = f"AND SCHEMA_NAME = '{schema}'" if schema else ""
        runner.cursor.execute(f"""
            SELECT PLAN_ID, STATEMENT_STRING, EXECUTION_COUNT, AVG_EXECUTION_TIME
            FROM SYS.M_SQL_PLAN_CACHE 
            WHERE STATEMENT_STRING LIKE '%SELECT%'
            {schema_filter}
            ORDER BY EXECUTION_COUNT DESC
            LIMIT 50
            WITH HINT (IGNORE_PLAN_CACHE)
        """)
        return runner.cursor.fetchall()
    except Exception as e:
        return []


def add_sql_log(sql, log_type="QUERY"):
    """Add an SQL log entry to session state."""
    if "sql_logs" not in st.session_state:
        st.session_state.sql_logs = []
    
    log_entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "type": log_type,
        "sql": sql
    }
    st.session_state.sql_logs.append(log_entry)
    
    # Keep only last 200 logs
    if len(st.session_state.sql_logs) > 200:
        st.session_state.sql_logs = st.session_state.sql_logs[-200:]


def execute_query_with_timing(runner, query_sql, demo_mode=False):
    if demo_mode:
        time.sleep(0.1)
        return {
            'execution_time': random.uniform(0.1, 2.0),
            'rows_affected': random.randint(100, 10000),
            'memory_used': random.randint(1000, 100000)
        }
    
    try:
        add_sql_log(query_sql, "QUERY")
        start_time = time.perf_counter()
        runner.cursor.execute(query_sql)
        result = runner.cursor.fetchall()
        end_time = time.perf_counter()
        
        execution_time = end_time - start_time
        rows_affected = len(result) if result else 0
        
        add_sql_log(f"Result: {rows_affected} rows in {execution_time:.3f}s", "BENCHMARK")
        
        return {
            'execution_time': execution_time,
            'rows_affected': rows_affected,
            'memory_used': 0,
            'results': result[:100] if result else []  # Store first 100 results
        }
    except Exception as e:
        add_sql_log(f"Error: {str(e)}", "ERROR")
        return {'error': str(e), 'execution_time': 0}


def connect_db(host, port, user, password):
    if any(emptystring(x) for x in [host, port, user, password]):
        st.error("Please provide all fields for database connection.")
        return

    try:
        with st.spinner("Connecting to database..."):
            st.session_state.runner.get_cursor(host, int(port), user, password, False)
            st.session_state.runner.setup()
    except Exception as e:
        st.error(f"Could not connect to the Database: {str(e)}")
        return
    
    st.session_state.connected = True
    st.session_state.schemas = list(sorted(st.session_state.runner.get_schemas()))
    st.success("Connected successfully!")


def main():
    assets_path = Path(__file__).resolve().parent.parent / "assets"
    
    try:
        with open(assets_path / "sap-logo-svg.svg") as f:
            sap_logo = f.read()
    except FileNotFoundError:
        sap_logo = ""

    css = """
        <style>
            .dependency-card {
                padding: 12px; margin: 8px 0; border-radius: 8px; border-left: 5px solid;
                background: linear-gradient(135deg, #f8f9fa 0%, #ffffff 100%);
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            .dep-badge {
                display: inline-block; padding: 3px 8px; border-radius: 12px;
                font-size: 0.75em; font-weight: bold; margin-right: 8px;
            }
            .fd-badge { background-color: #4CAF50; color: white; }
            .od-badge { background-color: #2196F3; color: white; }
            .valid-badge { background-color: #28a745; color: white; }
            .invalid-badge { background-color: #dc3545; color: white; }
            .manual-badge { background-color: #007bff; color: white; }
            .user-validated-badge { background-color: #9C27B0; color: white; }
            .step-header {
                display: flex; align-items: center; margin-bottom: 10px;
            }
            .step-number {
                background-color: #007bff; color: white; width: 32px; height: 32px;
                border-radius: 50%; display: flex; align-items: center; justify-content: center;
                font-weight: bold; margin-right: 12px; font-size: 16px;
            }
            .step-title { font-size: 1.2em; font-weight: 600; margin: 0; }
            .dep-separator { border-top: 1px solid #dee2e6; margin: 15px 0; }
        </style>
        """

    # Header with logos
    hpi_srgb_path = assets_path / "hpi_logo_srgb_b.svg"
    hpi_logo_b64 = base64.b64encode(open(str(hpi_srgb_path), 'rb').read()).decode() if hpi_srgb_path.exists() else ""
    
    st.markdown(f'''
    <div style="display: flex; align-items: center; gap: 20px;">
        <div style="display: flex; align-items: center; gap: 15px;">
            <img src="data:image/svg+xml;base64,{hpi_logo_b64}" height="50" alt="HPI Logo" style="height: 50px; width: auto;" />
            <svg viewbox="0 0 100 50" height="50" style="height: 50px; width: auto;">{sap_logo}</svg>
        </div>
        <h1 style="margin: 0;"><span style="font-variant-caps: small-caps;">HaDA</span>: HANA Data Dependency Assistant</h1>
    </div>
    ''', unsafe_allow_html=True)

    if "cnt" not in st.session_state:
        st.session_state.rewritten_query = ""
        st.session_state.cnt = 0
        st.session_state.has_query = False
        st.session_state.schemas = ["SYSTEM"]
        st.session_state.connected = False
        st.session_state.query_input = UserInputState()
        st.session_state.runner = DependencyDiscoveryRunner(["SYSTEM"], True, True, "query", 1000)
        st.session_state.query_render_key = 0
        st.session_state.original_plan = None
        st.session_state.rewritten_plan = None
        st.session_state.selected_dep_colors = {}
        st.session_state.candidate_deps = []
        st.session_state.validated_candidates = set()
        st.session_state.manually_added_deps = set()
        st.session_state.selected_dependencies_set = set()
        # Performance history for bar chart comparisons
        st.session_state.performance_history = []
        st.session_state.show_query_results = False
        st.session_state.last_query_results = {'original': [], 'rewritten': []}

    from query_plan import Column
    
    # Clear All button at the top
    clear_col1, clear_col2 = st.columns([4, 1])
    with clear_col1:
        demo_mode = st.checkbox("Demo mode: Use example dependencies")
    with clear_col2:
        if st.button("Clear All", type="secondary", use_container_width=True):
            # Reset all state
            reset_dependency_state()
            st.session_state.workflow_state = "init"
            st.session_state.candidate_deps = []
            st.session_state.user_validated_fds = {}
            st.session_state.user_validated_ods = {}
            st.session_state.query_input.current = ""
            st.session_state.query_input.previous = ""
            st.session_state.has_query = False
            st.session_state.query_render_key += 1
            st.session_state.performance_history = []
            st.session_state.current_original_times = []
            st.session_state.current_rewritten_times = []
            st.session_state.benchmark_running = False
            st.session_state.sql_logs = []
            st.session_state.last_query_results = {'original': [], 'rewritten': []}
            # Keep prev_demo_query matching current selection so it won't re-trigger loading
            # (handled below after rerun)
            st.session_state.clear_all_triggered = True
            # Also clear pending demo data so it won't auto-load
            st.session_state.pending_demo_fds = {}
            st.session_state.pending_demo_ods = {}
            st.session_state.pending_demo_candidates = []
            st.session_state.just_loaded_demo_query = False
            # Clear runner data
            if hasattr(st.session_state, 'runner'):
                st.session_state.runner.valid_fds.clear()
                st.session_state.runner.valid_ods.clear()
            st.rerun()

    # Demo queries with query plan variants based on selected dependencies
    # TSSJ = TABLE SCAN SEMI JOIN
    # OD enables BETWEEN predicates, FD enables converting HASH JOINs to TSSJs
    demo_queries_dict = {
        "TPCDS Catalog Returns Complex Join": {
            "query": """SELECT cr_order_number, cr_item_sk
FROM catalog_returns_sanitized
    INNER MANY TO ONE JOIN catalog_sales_sanitized
        ON cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk
    INNER MANY TO ONE JOIN date_dim ON cs_sold_date_sk = d_date_sk
    INNER MANY TO ONE JOIN customer ON cs_bill_customer_sk = c_customer_sk
    INNER MANY TO ONE JOIN customer_address ON c_current_addr_sk = ca_address_sk
WHERE d_year BETWEEN 2000 AND 2002
    AND ca_country = 'United States'
    AND d_moy >= 6;""",
            "fds": {
                # FD cs_order_number -> ... enables HASH JOIN -> TSSJ transformation
                Column("catalog_sales_sanitized", "cs_order_number"): {
                    Column("catalog_sales_sanitized", "cs_bill_customer_sk"),
                    Column("catalog_sales_sanitized", "cs_sold_date_sk")
                },
            },
            "ods": {
                # OD d_year, d_moy |-> d_date_sk: enables BETWEEN predicate
                Column("date_dim", "d_date_sk"): {(Column("date_dim", "d_year"), Column("date_dim", "d_moy"))},
                # OD cs_order_number |-> cs_sold_date_sk: enables BETWEEN on cs_item_sk
                Column("catalog_sales_sanitized", "cs_order_number"): {(Column("catalog_sales_sanitized", "cs_sold_date_sk"),)},
            },
            "candidates": [
                ("FD", Column("customer", "c_customer_sk"), {Column("customer", "c_customer_id")}),
                ("OD", Column("customer_address", "ca_address_sk"), [Column("customer_address", "ca_country")]),
            ],
            # Original plan without hints: HASH JOIN for fact table join, TSSJs for dimensions
            "original_plan": {
                'nodes': [
                    {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'cr_order_number, cr_item_sk', 'table': None, 'children': []},
                    # HASH JOIN: catalog_returns <-> catalog_sales (no FD known)
                    {'id': 2, 'parent_id': 1, 'name': 'HASH JOIN', 'details': 'cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk', 'table': None, 'children': []},
                    {'id': 3, 'parent_id': 2, 'name': 'catalog_returns_sanitized', 'details': None, 'table': 'catalog_returns_sanitized', 'children': []},
                    {'id': 4, 'parent_id': 2, 'name': 'TSSJ', 'details': 'cs_bill_customer_sk = c_customer_sk', 'table': 'customer', 'children': []},
                    # TSSJ on catalog_sales_sanitized (probe), date_dim is build side
                    {'id': 5, 'parent_id': 4, 'name': 'TSSJ', 'details': 'cs_sold_date_sk = d_date_sk', 'table': 'catalog_sales_sanitized', 'children': []},
                    {'id': 6, 'parent_id': 5, 'name': 'catalog_sales_sanitized', 'details': None, 'table': 'catalog_sales_sanitized', 'children': []},
                    {'id': 7, 'parent_id': 5, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                    # TSSJ on customer (probe), customer_address is build side
                    {'id': 8, 'parent_id': 4, 'name': 'TSSJ', 'details': 'c_current_addr_sk = ca_address_sk', 'table': 'customer', 'children': []},
                    {'id': 9, 'parent_id': 8, 'name': 'customer', 'details': None, 'table': 'customer', 'children': []},
                    {'id': 10, 'parent_id': 8, 'name': 'TABLE SCAN', 'details': None, 'table': 'customer_address', 'children': []},
                ],
                'root': []
            },
            # Rewritten plan variants depending on which dependencies are selected
            "rewritten_plans": {
                "od_date": {
                    # OD d_year, d_moy -> d_date_sk: TSSJ gets BETWEEN predicate
                    'nodes': [
                        {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'cr_order_number, cr_item_sk', 'table': None, 'children': []},
                        {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 3, 'parent_id': 2, 'name': 'catalog_returns_sanitized', 'details': None, 'table': 'catalog_returns_sanitized', 'children': []},
                        {'id': 4, 'parent_id': 2, 'name': 'TSSJ', 'details': 'cs_bill_customer_sk = c_customer_sk', 'table': 'customer', 'children': []},
                        {'id': 5, 'parent_id': 4, 'name': 'TSSJ', 'details': 'cs_sold_date_sk BETWEEN MIN(d_date_sk) AND MAX(d_date_sk)', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 6, 'parent_id': 5, 'name': 'catalog_sales_sanitized', 'details': None, 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 7, 'parent_id': 5, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                        {'id': 8, 'parent_id': 4, 'name': 'TSSJ', 'details': 'c_current_addr_sk = ca_address_sk', 'table': 'customer', 'children': []},
                        {'id': 9, 'parent_id': 8, 'name': 'customer', 'details': None, 'table': 'customer', 'children': []},
                        {'id': 10, 'parent_id': 8, 'name': 'TABLE SCAN', 'details': None, 'table': 'customer_address', 'children': []},
                    ],
                    'root': []
                },
                "fd_cs_order": {
                    # FD cs_order_number: HASH JOIN -> TSSJ
                    'nodes': [
                        {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'cr_order_number, cr_item_sk', 'table': None, 'children': []},
                        {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 3, 'parent_id': 2, 'name': 'catalog_returns_sanitized', 'details': None, 'table': 'catalog_returns_sanitized', 'children': []},
                        {'id': 4, 'parent_id': 2, 'name': 'TSSJ', 'details': 'cs_bill_customer_sk = c_customer_sk', 'table': 'customer', 'children': []},
                        {'id': 5, 'parent_id': 4, 'name': 'TSSJ', 'details': 'cs_sold_date_sk = d_date_sk', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 6, 'parent_id': 5, 'name': 'catalog_sales_sanitized', 'details': None, 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 7, 'parent_id': 5, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                        {'id': 8, 'parent_id': 4, 'name': 'TSSJ', 'details': 'c_current_addr_sk = ca_address_sk', 'table': 'customer', 'children': []},
                        {'id': 9, 'parent_id': 8, 'name': 'customer', 'details': None, 'table': 'customer', 'children': []},
                        {'id': 10, 'parent_id': 8, 'name': 'TABLE SCAN', 'details': None, 'table': 'customer_address', 'children': []},
                    ],
                    'root': []
                },
                "od_cs_order": {
                    # OD cs_order_number -> cs_sold_date_sk: TSSJ with BETWEEN on cs_item_sk
                    'nodes': [
                        {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'cr_order_number, cr_item_sk', 'table': None, 'children': []},
                        {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'cr_order_number = cs_order_number AND cs_item_sk BETWEEN MIN AND MAX', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 3, 'parent_id': 2, 'name': 'catalog_returns_sanitized', 'details': None, 'table': 'catalog_returns_sanitized', 'children': []},
                        {'id': 4, 'parent_id': 2, 'name': 'TSSJ', 'details': 'cs_bill_customer_sk = c_customer_sk', 'table': 'customer', 'children': []},
                        {'id': 5, 'parent_id': 4, 'name': 'TSSJ', 'details': 'cs_sold_date_sk = d_date_sk', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 6, 'parent_id': 5, 'name': 'catalog_sales_sanitized', 'details': None, 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 7, 'parent_id': 5, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                        {'id': 8, 'parent_id': 4, 'name': 'TSSJ', 'details': 'c_current_addr_sk = ca_address_sk', 'table': 'customer', 'children': []},
                        {'id': 9, 'parent_id': 8, 'name': 'customer', 'details': None, 'table': 'customer', 'children': []},
                        {'id': 10, 'parent_id': 8, 'name': 'TABLE SCAN', 'details': None, 'table': 'customer_address', 'children': []},
                    ],
                    'root': []
                },
                "od_date_fd_cs": {
                    # OD date + FD cs_order: TSSJ with BETWEEN
                    'nodes': [
                        {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'cr_order_number, cr_item_sk', 'table': None, 'children': []},
                        {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 3, 'parent_id': 2, 'name': 'catalog_returns_sanitized', 'details': None, 'table': 'catalog_returns_sanitized', 'children': []},
                        {'id': 4, 'parent_id': 2, 'name': 'TSSJ', 'details': 'cs_bill_customer_sk = c_customer_sk', 'table': 'customer', 'children': []},
                        {'id': 5, 'parent_id': 4, 'name': 'TSSJ', 'details': 'cs_sold_date_sk BETWEEN MIN(d_date_sk) AND MAX(d_date_sk)', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 6, 'parent_id': 5, 'name': 'catalog_sales_sanitized', 'details': None, 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 7, 'parent_id': 5, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                        {'id': 8, 'parent_id': 4, 'name': 'TSSJ', 'details': 'c_current_addr_sk = ca_address_sk', 'table': 'customer', 'children': []},
                        {'id': 9, 'parent_id': 8, 'name': 'customer', 'details': None, 'table': 'customer', 'children': []},
                        {'id': 10, 'parent_id': 8, 'name': 'TABLE SCAN', 'details': None, 'table': 'customer_address', 'children': []},
                    ],
                    'root': []
                },
                "all": {
                    # all dependencies selected: BETWEEN on both TSSJs
                    'nodes': [
                        {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'cr_order_number, cr_item_sk', 'table': None, 'children': []},
                        {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'cr_order_number = cs_order_number AND cs_item_sk BETWEEN MIN AND MAX', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 3, 'parent_id': 2, 'name': 'catalog_returns_sanitized', 'details': None, 'table': 'catalog_returns_sanitized', 'children': []},
                        {'id': 4, 'parent_id': 2, 'name': 'TSSJ', 'details': 'cs_bill_customer_sk = c_customer_sk', 'table': 'customer', 'children': []},
                        {'id': 5, 'parent_id': 4, 'name': 'TSSJ', 'details': 'cs_sold_date_sk BETWEEN MIN(d_date_sk) AND MAX(d_date_sk)', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 6, 'parent_id': 5, 'name': 'catalog_sales_sanitized', 'details': None, 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 7, 'parent_id': 5, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                        {'id': 8, 'parent_id': 4, 'name': 'TSSJ', 'details': 'c_current_addr_sk = ca_address_sk', 'table': 'customer', 'children': []},
                        {'id': 9, 'parent_id': 8, 'name': 'customer', 'details': None, 'table': 'customer', 'children': []},
                        {'id': 10, 'parent_id': 8, 'name': 'TABLE SCAN', 'details': None, 'table': 'customer_address', 'children': []},
                    ],
                    'root': []
                },
                "none": {
                    # no dependencies selected: same as original plan
                    'nodes': [
                        {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'cr_order_number, cr_item_sk', 'table': None, 'children': []},
                        {'id': 2, 'parent_id': 1, 'name': 'HASH JOIN', 'details': 'cr_order_number = cs_order_number AND cs_item_sk = cr_item_sk', 'table': None, 'children': []},
                        {'id': 3, 'parent_id': 2, 'name': 'catalog_returns_sanitized', 'details': None, 'table': 'catalog_returns_sanitized', 'children': []},
                        {'id': 4, 'parent_id': 2, 'name': 'TSSJ', 'details': 'cs_bill_customer_sk = c_customer_sk', 'table': 'customer', 'children': []},
                        {'id': 5, 'parent_id': 4, 'name': 'TSSJ', 'details': 'cs_sold_date_sk = d_date_sk', 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 6, 'parent_id': 5, 'name': 'catalog_sales_sanitized', 'details': None, 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 7, 'parent_id': 5, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                        {'id': 8, 'parent_id': 4, 'name': 'TSSJ', 'details': 'c_current_addr_sk = ca_address_sk', 'table': 'customer', 'children': []},
                        {'id': 9, 'parent_id': 8, 'name': 'customer', 'details': None, 'table': 'customer', 'children': []},
                        {'id': 10, 'parent_id': 8, 'name': 'TABLE SCAN', 'details': None, 'table': 'customer_address', 'children': []},
                    ],
                    'root': []
                },
            },
            "demo_performance": {
                "original_avg": 2.34,
                "original_std": 0.45,
                "rewritten_avg": 0.89,
                "rewritten_std": 0.12
            }
        },
        "TPCDS Catalog Sales Date Join": {
            "query": """SELECT cs_sold_date_sk, cs_item_sk
FROM catalog_sales_sanitized
    INNER MANY TO ONE JOIN date_dim ON cs_sold_date_sk = d_date_sk
WHERE d_year = 2001 AND d_moy > 2;""",
            "fds": {},
            "ods": {
                # OD d_year, d_moy -> d_date_sk
                Column("date_dim", "d_date_sk"): {(Column("date_dim", "d_year"), Column("date_dim", "d_moy"))}
            },
            "candidates": [
                ("FD", Column("catalog_sales_sanitized", "cs_order_number"), {Column("catalog_sales_sanitized", "cs_warehouse_sk")}),
                ("OD", Column("date_dim", "d_date_sk"), [Column("date_dim", "d_week_seq")]),
            ],
            # Original plan: TSSJ with probe table and TABLE SCAN on dimension
            "original_plan": {
                'nodes': [
                    {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'cs_sold_date_sk, cs_item_sk', 'table': None, 'children': []},
                    {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'cs_sold_date_sk = d_date_sk', 'table': 'date_dim', 'children': []},
                    {'id': 3, 'parent_id': 2, 'name': 'catalog_sales_sanitized', 'details': None, 'table': 'catalog_sales_sanitized', 'children': []},
                    {'id': 4, 'parent_id': 2, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                ],
                'root': []
            },
            "rewritten_plans": {
                "od_date": {
                    'nodes': [
                        {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'cs_sold_date_sk, cs_item_sk', 'table': None, 'children': []},
                        {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'cs_sold_date_sk BETWEEN MIN(d_date_sk) AND MAX(d_date_sk)', 'table': 'date_dim', 'children': []},
                        {'id': 3, 'parent_id': 2, 'name': 'catalog_sales_sanitized', 'details': None, 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 4, 'parent_id': 2, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                    ],
                    'root': []
                },
                "none": {
                    'nodes': [
                        {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'cs_sold_date_sk, cs_item_sk', 'table': None, 'children': []},
                        {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'cs_sold_date_sk = d_date_sk', 'table': 'date_dim', 'children': []},
                        {'id': 3, 'parent_id': 2, 'name': 'catalog_sales_sanitized', 'details': None, 'table': 'catalog_sales_sanitized', 'children': []},
                        {'id': 4, 'parent_id': 2, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                    ],
                    'root': []
                },
            },
            "demo_performance": {
                "original_avg": 1.56,
                "original_std": 0.32,
                "rewritten_avg": 0.67,
                "rewritten_std": 0.08
            }
        },
        "TPCDS Store Sales Analysis": {
            "query": """SELECT ss_sold_date_sk, ss_store_sk
FROM store_sales
    INNER MANY TO ONE JOIN store ON ss_store_sk = s_store_sk
    INNER MANY TO ONE JOIN date_dim ON ss_sold_date_sk = d_date_sk
WHERE d_year = 2002 AND s_state = 'TN';""",
            "fds": {},
            "ods": {
                # OD d_year -> d_date_sk
                Column("date_dim", "d_date_sk"): {(Column("date_dim", "d_year"),)},
            },
            "candidates": [
                ("FD", Column("store", "s_store_sk"), {Column("store", "s_zip")}),
                ("OD", Column("store", "s_store_sk"), [Column("store", "s_state")]),
            ],
            # Original plan: 2 TSSJs chained
            "original_plan": {
                'nodes': [
                    {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'ss_sold_date_sk, ss_store_sk', 'table': None, 'children': []},
                    {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'ss_sold_date_sk = d_date_sk', 'table': 'date_dim', 'children': []},
                    {'id': 3, 'parent_id': 2, 'name': 'TSSJ', 'details': 'ss_store_sk = s_store_sk', 'table': 'store', 'children': []},
                    {'id': 4, 'parent_id': 3, 'name': 'store_sales', 'details': None, 'table': 'store_sales', 'children': []},
                    {'id': 5, 'parent_id': 3, 'name': 'TABLE SCAN', 'details': None, 'table': 'store', 'children': []},
                    {'id': 6, 'parent_id': 2, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                ],
                'root': []
            },
            "rewritten_plans": {
                "od_date": {
                    'nodes': [
                        {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'ss_sold_date_sk, ss_store_sk', 'table': None, 'children': []},
                        {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'ss_sold_date_sk BETWEEN MIN(d_date_sk) AND MAX(d_date_sk)', 'table': 'date_dim', 'children': []},
                        {'id': 3, 'parent_id': 2, 'name': 'TSSJ', 'details': 'ss_store_sk = s_store_sk', 'table': 'store', 'children': []},
                        {'id': 4, 'parent_id': 3, 'name': 'store_sales', 'details': None, 'table': 'store_sales', 'children': []},
                        {'id': 5, 'parent_id': 3, 'name': 'TABLE SCAN', 'details': None, 'table': 'store', 'children': []},
                        {'id': 6, 'parent_id': 2, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                    ],
                    'root': []
                },
                "none": {
                    'nodes': [
                        {'id': 1, 'parent_id': None, 'name': 'PROJECT', 'details': 'ss_sold_date_sk, ss_store_sk', 'table': None, 'children': []},
                        {'id': 2, 'parent_id': 1, 'name': 'TSSJ', 'details': 'ss_sold_date_sk = d_date_sk', 'table': 'date_dim', 'children': []},
                        {'id': 3, 'parent_id': 2, 'name': 'TSSJ', 'details': 'ss_store_sk = s_store_sk', 'table': 'store', 'children': []},
                        {'id': 4, 'parent_id': 3, 'name': 'store_sales', 'details': None, 'table': 'store_sales', 'children': []},
                        {'id': 5, 'parent_id': 3, 'name': 'TABLE SCAN', 'details': None, 'table': 'store', 'children': []},
                        {'id': 6, 'parent_id': 2, 'name': 'TABLE SCAN', 'details': None, 'table': 'date_dim', 'children': []},
                    ],
                    'root': []
                },
            },
            "demo_performance": {
                "original_avg": 1.89,
                "original_std": 0.28,
                "rewritten_avg": 0.72,
                "rewritten_std": 0.09
            }
        },
    }
    
    selected_demo_query = None
    if demo_mode:
        # Default to the complex query
        demo_keys = list(demo_queries_dict.keys())
        default_idx = demo_keys.index("TPCDS Catalog Returns Complex Join") if "TPCDS Catalog Returns Complex Join" in demo_keys else 0
        selected_demo_query = st.selectbox("Choose demo query:", demo_keys, index=default_idx, key="demo_query_select_top")
        runner = st.session_state.runner
        prev_demo_query = st.session_state.get("prev_demo_query")
        
        # If clear all was just triggered, don't auto-load the query
        if st.session_state.get("clear_all_triggered"):
            st.session_state.clear_all_triggered = False
            st.session_state.prev_demo_query = selected_demo_query  # Sync so next change is detected
        elif prev_demo_query != selected_demo_query:
            # Reset state when demo query changes - but DON'T load dependencies yet
            reset_dependency_state()
            # Also reset workflow_state so dependencies don't show until Discover is clicked
            st.session_state.workflow_state = "init"
            st.session_state.candidate_deps = []
            st.session_state.user_validated_fds = {}
            st.session_state.user_validated_ods = {}
            demo_entry = demo_queries_dict[selected_demo_query]
            # Store demo data for later use when "Discover" is clicked
            st.session_state.pending_demo_fds = demo_entry["fds"]
            st.session_state.pending_demo_ods = demo_entry["ods"]
            st.session_state.pending_demo_candidates = demo_entry.get("candidates", [])
            demo_query = demo_entry["query"]
            st.session_state.query_input.current = demo_query
            st.session_state.has_query = True
            st.session_state.just_loaded_demo_query = True
            st.session_state.query_render_key += 1
            st.session_state.prev_demo_query = selected_demo_query
            st.info("Demo query loaded. Click 'Discover Dependencies' to discover.")
    else:
        runner = st.session_state.runner

    # Sidebar
    with st.sidebar:
        st.subheader("HANA Instance")
        db_host = st.text_input("Host", placeholder="host.company.com")
        db_port = st.text_input("Port", placeholder="3001")
        db_user = st.text_input("User", placeholder="SYSTEM")
        db_password = st.text_input("Password", type="password")
        st.button("Connect", use_container_width=True, on_click=connect_db, args=(db_host, db_port, db_user, db_password))
        selected_schema = st.selectbox("Schema", st.session_state.schemas, disabled=(not st.session_state.connected))
        
        st.subheader("Configuration")
        enable_fd = st.checkbox("FD-based rewrites", value=True, disabled=(not st.session_state.connected))
        enable_od = st.checkbox("OD-based rewrites", value=True, disabled=(not st.session_state.connected))
        
        if st.session_state.connected:
            st.session_state.runner.fd_rewrite = enable_fd
            st.session_state.runner.od_rewrite = enable_od
        
        st.markdown("---")
        st.subheader("Plan Cache")
        if st.button("Load from Cache", disabled=not st.session_state.connected, use_container_width=True):
            cached_queries = get_cached_queries(runner, selected_schema)
            if cached_queries:
                st.session_state.cached_queries = cached_queries
                st.success(f"Loaded {len(cached_queries)} queries from cache")
            else:
                st.warning("No cached queries found")
        
        if st.session_state.get("cached_queries"):
            query_options = [f"{q[0][:8]}... ({q[2]} exec)" for q in st.session_state.cached_queries[:10]]
            selected_cache_idx = st.selectbox("Select cached query:", range(len(query_options)), format_func=lambda x: query_options[x])
            if st.button("Use Selected", use_container_width=True):
                reset_dependency_state()
                st.session_state.query_input.current = st.session_state.cached_queries[selected_cache_idx][1]
                st.session_state.query_render_key += 1
                st.rerun()

    # ==================== STEP 1: SQL Query Input ====================
    st.markdown("---")
    st.markdown('<div class="step-header"><div class="step-number">1</div><div class="step-title">SQL Query Input</div></div>', unsafe_allow_html=True)
    
    c1, c2 = st.columns([1, 1], gap="large")

    with c1:
        a, b = st.columns([3, 1], vertical_alignment="center")
        a.markdown("**Original Query**")
        reset_query_btn = b.button("Reset Query", use_container_width=True, disabled=not st.session_state.query_input.current.strip())
        
        if reset_query_btn:
            reset_dependency_state()
            st.session_state.query_input.current = ""
            st.session_state.query_input.previous = ""
            st.session_state.has_query = False
            st.session_state.query_render_key += 1
            st.rerun()

        q = st_ace(
            language="sql",
            value=st.session_state.query_input.current,
            show_gutter=True,
            wrap=True,
            tab_size=4,
            key=f"qOriginal-{st.session_state.query_render_key}",
            auto_update=True,
            height=200,
        )
        
        if not st.session_state.get("just_loaded_demo_query"):
            # Check if query changed and reset state
            if q != st.session_state.query_input.current:
                st.session_state.query_input.current = q
                if st.session_state.query_input.has_changed():
                    reset_dependency_state()
                    st.session_state.query_input.advance()
        else:
            st.session_state.just_loaded_demo_query = False
            
        st.session_state.has_query = not emptystring(st.session_state.query_input.current)

        if st.button("Discover Dependencies", disabled=not st.session_state.query_input.current.strip(), type="primary"):
            st.session_state.workflow_state = "dependencies"
            if st.session_state.connected:
                with st.spinner("Discovering dependencies..."):
                    try:
                        st.session_state.runner.run_single_query(st.session_state.query_input.current)
                        st.success("Dependencies discovered!")
                    except Exception as e:
                        st.error(f"Error discovering dependencies: {str(e)}")
            elif demo_mode:
                # Load the pending demo dependencies now
                if st.session_state.get("pending_demo_fds"):
                    for k, v in st.session_state.pending_demo_fds.items():
                        runner.valid_fds[k] = v
                if st.session_state.get("pending_demo_ods"):
                    for k, v in st.session_state.pending_demo_ods.items():
                        runner.valid_ods[k] = v
                if st.session_state.get("pending_demo_candidates"):
                    st.session_state.candidate_deps = st.session_state.pending_demo_candidates
                st.success("Demo dependencies discovered!")
                st.rerun()

    with c2:
        a, b = st.columns([3, 1], vertical_alignment="center")
        a.markdown("**Rewritten Query**")
        reset_button = b.button("Reset Rewrite", use_container_width=True, disabled=not st.session_state.get("rewritten_query"))
        
        if reset_button:
            st.session_state.rewritten_query = ""
            st.session_state.selected_dependencies_set = set()
            st.session_state.selected_dep_colors = {}
            st.session_state.rewritten_plan = None
            st.rerun()

        if st.session_state.get("rewritten_query"):
            rewritten_query = st.session_state.rewritten_query
            
            # Use SNAPSHOT colors from rewrite time, not current selection colors
            rewritten_colors = st.session_state.get("rewritten_dep_colors", {})
            rewritten_deps = st.session_state.get("rewritten_dependencies_set", set())
            
            if rewritten_colors and rewritten_deps:
                hint_match = re.search(r"(DEV_DATA_DEPENDENCIES\s*\(\s*'\s*\[)(.*?)(\]\s*'\s*\))", rewritten_query, re.DOTALL | re.IGNORECASE)
                
                if hint_match:
                    before_hint = rewritten_query[:hint_match.start()]
                    hint_prefix = hint_match.group(1)
                    hint_content = hint_match.group(2)
                    hint_suffix = hint_match.group(3)
                    after_hint = rewritten_query[hint_match.end():]
                    
                    entry_pattern = r'\{[^}]+\}'
                    entries = re.findall(entry_pattern, hint_content)
                    selected_list = list(rewritten_deps)  # Use snapshot deps
                    
                    colored_entries = []
                    for entry_idx, entry in enumerate(entries):
                        if entry_idx < len(selected_list):
                            label = selected_list[entry_idx]
                            color_idx = rewritten_colors.get(label, entry_idx)  # Use snapshot colors
                            color = DEPENDENCY_COLORS[color_idx % len(DEPENDENCY_COLORS)]
                            entry_escaped = entry.replace("<", "&lt;").replace(">", "&gt;")
                            colored_entries.append(f'<span style="background-color: {color}; padding: 2px 4px; border-radius: 4px; color: #000;">{entry_escaped}</span>')
                        else:
                            colored_entries.append(entry.replace("<", "&lt;").replace(">", "&gt;"))
                    
                    colored_hint_content = ", ".join(colored_entries)
                    before_hint_escaped = before_hint.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
                    hint_prefix_escaped = hint_prefix.replace("<", "&lt;").replace(">", "&gt;")
                    hint_suffix_escaped = hint_suffix.replace("<", "&lt;").replace(">", "&gt;")
                    after_hint_escaped = after_hint.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
                    
                    html_query = f"""<div style="background-color: #f5f5f5; border: 1px solid #ddd; border-radius: 8px; padding: 15px; font-family: monospace; font-size: 12px; line-height: 1.5; overflow-x: auto; white-space: pre-wrap;">
<span style="color: #0066cc;">{before_hint_escaped}</span><span style="color: #666;">{hint_prefix_escaped}</span>{colored_hint_content}<span style="color: #666;">{hint_suffix_escaped}</span><span style="color: #0066cc;">{after_hint_escaped}</span></div>"""
                    st.markdown(html_query, unsafe_allow_html=True)
                else:
                    st.code(rewritten_query, language="sql")
            else:
                st.code(rewritten_query, language="sql")
        else:
            st.info("No rewritten query yet. Select dependencies below and click 'Apply Rewrite'.")

    # ==================== STEP 2: Dependency Selection ====================
    st.markdown("---")
    st.markdown('<div class="step-header"><div class="step-number">2</div><div class="step-title">Dependency Selection</div></div>', unsafe_allow_html=True)
    
    if "workflow_state" not in st.session_state:
        st.session_state.workflow_state = "init"
    
    # Only show dependencies section after discovery (not just because demo mode is on)
    show_dependencies = st.session_state.workflow_state in ("dependencies", "rewrite", "approved", "plan")
    
    if show_dependencies:
        # Build valid dependencies list - showing user-validated separately
        valid_dependencies = []
        dep_details = {}
        fd_count = 0
        od_count = 0
        
        # Get user-validated dependency labels to exclude from merged display
        user_val_labels = st.session_state.get("manually_added_deps", set())
        user_val_fds = st.session_state.get("user_validated_fds", {})
        user_val_ods = st.session_state.get("user_validated_ods", {})
        
        # First add original (auto-discovered) FDs
        if runner.valid_fds:
            for lhs, rhs_set in runner.valid_fds.items():
                # Get the original RHS columns (exclude user-validated ones)
                original_rhs = set()
                for rhs_col in rhs_set:
                    is_user_added = False
                    for uv_label, (uv_lhs, uv_rhs) in user_val_fds.items():
                        if uv_lhs == lhs and rhs_col in uv_rhs:
                            is_user_added = True
                            break
                    if not is_user_added:
                        original_rhs.add(rhs_col)
                
                # Add original dependency if it has columns
                if original_rhs:
                    label = f"FD: {lhs} -> {{{', '.join(str(col) for col in original_rhs)}}}"
                    valid_dependencies.append(label)
                    dep_details[label] = ('FD', lhs, original_rhs)
                    fd_count += 1
        
        # Add user-validated FDs as separate entries
        for uv_label, (uv_lhs, uv_rhs) in user_val_fds.items():
            if uv_label not in [d for d in valid_dependencies]:
                valid_dependencies.append(uv_label)
                dep_details[uv_label] = ('FD', uv_lhs, uv_rhs)
                fd_count += 1
        
        # Handle ODs
        if runner.valid_ods:
            for lhs, rhs_set in runner.valid_ods.items():
                for rhs in rhs_set:
                    if isinstance(rhs, (list, tuple)):
                        rhs_str = ' => '.join(str(col) for col in rhs)
                    else:
                        rhs_str = str(rhs)
                    label = f"OD: {lhs} ↦ [{rhs_str}]"
                    # Skip if this was user-validated (will be added separately)
                    if label not in user_val_labels or label not in user_val_ods:
                        valid_dependencies.append(label)
                        dep_details[label] = ('OD', lhs, rhs)
                        od_count += 1
        
        # Add user-validated ODs as separate entries  
        for uv_label, (uv_lhs, uv_rhs) in user_val_ods.items():
            if uv_label not in [d for d in valid_dependencies]:
                valid_dependencies.append(uv_label)
                dep_details[uv_label] = ('OD', uv_lhs, uv_rhs)
                od_count += 1
        
        # Summary metrics
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Valid Dependencies", len(valid_dependencies))
        with col2:
            st.metric("Functional (FD)", fd_count)
        with col3:
            st.metric("Order (OD)", od_count)
        with col4:
            st.metric("Selected", len(st.session_state.get("selected_dependencies_set", set())))
        
        dep_col, select_col = st.columns([2, 1])
        
        with dep_col:
            st.markdown("#### Valid Dependencies")
            st.markdown("*These dependencies have been validated. Click to select for query rewriting.*")
            
            if valid_dependencies:
                # Group dependencies by table name
                deps_by_table = defaultdict(list)
                for idx, label in enumerate(valid_dependencies):
                    dtype, lhs, rhs = dep_details[label]
                    table_name = lhs.table_name if hasattr(lhs, 'table_name') else "Unknown"
                    deps_by_table[table_name].append((idx, label, dtype, lhs, rhs))
                
                # Display dependencies grouped by table
                global_idx = 0
                for table_name in sorted(deps_by_table.keys()):
                    # Table header
                    st.markdown(f'<div style="background-color: #e9ecef; padding: 8px 12px; margin: 12px 0 8px 0; border-radius: 4px; font-weight: bold; color: #495057;">{table_name}</div>', unsafe_allow_html=True)
                    
                    for idx, label, dtype, lhs, rhs in deps_by_table[table_name]:
                        is_selected = label in st.session_state.get("selected_dependencies_set", set())
                        color_idx = st.session_state.selected_dep_colors.get(label, -1)
                        is_user_validated = label in st.session_state.get("manually_added_deps", set())
                        
                        dep_row = st.columns([0.06, 0.59, 0.35])
                        
                        with dep_row[0]:
                            if is_selected and color_idx >= 0:
                                color = DEPENDENCY_COLORS[color_idx % len(DEPENDENCY_COLORS)]
                                st.markdown(f'<div style="background-color: {color}; width: 20px; height: 20px; border-radius: 50%;"></div>', unsafe_allow_html=True)
                            else:
                                st.markdown('<div style="width: 20px; height: 20px; border: 2px solid #ccc; border-radius: 50%;"></div>', unsafe_allow_html=True)
                        
                        with dep_row[1]:
                            badge_class = "fd-badge" if dtype == "FD" else "od-badge"
                            manual_badge = '<span class="dep-badge manual-badge">MANUAL</span>' if is_selected else ""
                            user_val_badge = '<span class="dep-badge user-validated-badge">USER VALIDATED</span>' if is_user_validated else '<span class="dep-badge valid-badge">VALID</span>'
                            # Only show column names 
                            lhs_col = lhs.column_name if hasattr(lhs, 'column_name') else str(lhs)
                            if isinstance(rhs, (set, frozenset, list, tuple)):
                                rhs_display = ", ".join(c.column_name if hasattr(c, 'column_name') else str(c) for c in rhs)
                            else:
                                rhs_display = rhs.column_name if hasattr(rhs, 'column_name') else str(rhs)
                            st.markdown(f'<span class="dep-badge {badge_class}">{dtype}</span>{user_val_badge}{manual_badge} <strong>{lhs_col}</strong> {"->" if dtype == "FD" else "↦"} <strong>{rhs_display}</strong>', unsafe_allow_html=True)
                        
                        with dep_row[2]:
                            # Show different buttons based on dependency state
                            if is_user_validated:
                                # User-validated deps: show Select/Deselect and Invalidate side by side
                                btn_col1, btn_col2 = st.columns(2)
                                btn_label = "Deselect" if is_selected else "Select"
                                btn_type = "primary" if is_selected else "secondary"
                                with btn_col1:
                                    if st.button(btn_label, key=f"dep_btn_{global_idx}", type=btn_type, use_container_width=True):
                                        if "selected_dependencies_set" not in st.session_state:
                                            st.session_state.selected_dependencies_set = set()
                                        
                                        if is_selected:
                                            st.session_state.selected_dependencies_set.discard(label)
                                            if label in st.session_state.selected_dep_colors:
                                                del st.session_state.selected_dep_colors[label]
                                            # Don't clear rewritten_query here - wait for Apply Rewrite
                                        else:
                                            st.session_state.selected_dependencies_set.add(label)
                                            used_colors = set(st.session_state.selected_dep_colors.values())
                                            for ci in range(len(DEPENDENCY_COLORS)):
                                                if ci not in used_colors:
                                                    st.session_state.selected_dep_colors[label] = ci
                                                    break
                                        st.rerun()
                                with btn_col2:
                                    if st.button("Invalidate", key=f"inv_btn_{global_idx}", type="secondary", use_container_width=True):
                                        # Remove from user-validated tracking
                                        if label in st.session_state.get("user_validated_fds", {}):
                                            uv_lhs, uv_rhs = st.session_state.user_validated_fds[label]
                                            del st.session_state.user_validated_fds[label]
                                            # Remove RHS columns from runner.valid_fds
                                            if uv_lhs in runner.valid_fds:
                                                for col in uv_rhs:
                                                    runner.valid_fds[uv_lhs].discard(col)
                                                if not runner.valid_fds[uv_lhs]:
                                                    del runner.valid_fds[uv_lhs]
                                        if label in st.session_state.get("user_validated_ods", {}):
                                            uv_lhs, uv_rhs = st.session_state.user_validated_ods[label]
                                            del st.session_state.user_validated_ods[label]
                                            # Remove from runner.valid_ods
                                            if uv_lhs in runner.valid_ods:
                                                runner.valid_ods[uv_lhs].discard(uv_rhs)
                                                if not runner.valid_ods[uv_lhs]:
                                                    del runner.valid_ods[uv_lhs]
                                        # Remove from manually added deps and selection
                                        st.session_state.manually_added_deps.discard(label)
                                        st.session_state.selected_dependencies_set.discard(label)
                                        if label in st.session_state.selected_dep_colors:
                                            del st.session_state.selected_dep_colors[label]
                                        # Re-add to candidate_deps so it appears in Invalid section
                                        # Parse the dependency from the label
                                        if dtype == "FD":
                                            # Add back to candidate_deps list
                                            if not any(c[1] == lhs and c[2] == rhs for c in st.session_state.get("candidate_deps", [])):
                                                if "candidate_deps" not in st.session_state:
                                                    st.session_state.candidate_deps = []
                                                st.session_state.candidate_deps.append((dtype, lhs, rhs))
                                        else:  # OD
                                            if not any(c[1] == lhs and c[2] == rhs for c in st.session_state.get("candidate_deps", [])):
                                                if "candidate_deps" not in st.session_state:
                                                    st.session_state.candidate_deps = []
                                                st.session_state.candidate_deps.append((dtype, lhs, rhs))
                                        
                                        # Also remove from validated_candidates if present
                                        cand_key = f"{dtype}_{lhs}_{rhs}"
                                        st.session_state.validated_candidates.discard(cand_key)
                                        
                                        st.success("Dependency moved back to Invalid!")
                                        st.rerun()
                            else:
                                # Regular valid deps just have Select/Deselect
                                btn_label = "Deselect" if is_selected else "Select"
                                btn_type = "primary" if is_selected else "secondary"
                                if st.button(btn_label, key=f"dep_btn_{global_idx}", type=btn_type, use_container_width=True):
                                    if "selected_dependencies_set" not in st.session_state:
                                        st.session_state.selected_dependencies_set = set()
                                    
                                    if is_selected:
                                        st.session_state.selected_dependencies_set.discard(label)
                                        if label in st.session_state.selected_dep_colors:
                                            del st.session_state.selected_dep_colors[label]
                                        # Don't clear rewritten_query here - wait for Apply Rewrite
                                    else:
                                        st.session_state.selected_dependencies_set.add(label)
                                        used_colors = set(st.session_state.selected_dep_colors.values())
                                        for ci in range(len(DEPENDENCY_COLORS)):
                                            if ci not in used_colors:
                                                st.session_state.selected_dep_colors[label] = ci
                                                break
                                    st.rerun()
                        global_idx += 1
                
                # Separator line between valid and unvalidated
                st.markdown('<div class="dep-separator"></div>', unsafe_allow_html=True)
                
                # Candidate/invalid dependencies section
                if st.session_state.get("candidate_deps"):
                    st.markdown("#### Invalid Dependencies")
                    st.markdown("*These dependencies need validation. Mark as valid to use them.*")
                    
                    for cidx, (ctype, clhs, crhs) in enumerate(st.session_state.candidate_deps):
                        cand_key = f"{ctype}_{clhs}_{crhs}"
                        is_validated = cand_key in st.session_state.validated_candidates
                        
                        if not is_validated:  # Only show invalid ones here
                            cand_row = st.columns([0.08, 0.72, 0.2])
                            
                            with cand_row[0]:
                                st.markdown('<div style="width: 20px; height: 20px; border: 2px dashed #999; border-radius: 50%;"></div>', unsafe_allow_html=True)
                            
                            with cand_row[1]:
                                badge_class = "fd-badge" if ctype == "FD" else "od-badge"
                                crhs_display = ", ".join(str(c) for c in crhs) if isinstance(crhs, (set, frozenset, list, tuple)) else str(crhs)
                                st.markdown(f'<span class="dep-badge {badge_class}">{ctype}</span><span class="dep-badge invalid-badge">INVALID</span> <strong>{clhs}</strong> {"->" if ctype == "FD" else "↦"} <strong>{crhs_display}</strong>', unsafe_allow_html=True)
                            
                            with cand_row[2]:
                                if st.button("Mark as valid", key=f"cand_btn_{cidx}", type="secondary", use_container_width=True):
                                    st.session_state.validated_candidates.add(cand_key)
                                    # Add to valid dependencies as a SEPARATE entry (use unique key)
                                    if ctype == "FD":
                                        # Create a unique key by using a tuple of (lhs, frozenset(rhs))
                                        # Store each FD separately by using the specific RHS columns
                                        manual_label = f"FD: {clhs} -> {{{', '.join(str(col) for col in crhs)}}}"
                                        # Store in a separate tracking dict for user-validated FDs
                                        if "user_validated_fds" not in st.session_state:
                                            st.session_state.user_validated_fds = {}
                                        st.session_state.user_validated_fds[manual_label] = (clhs, crhs)
                                        # Also add to runner for hint generation
                                        if clhs not in runner.valid_fds:
                                            runner.valid_fds[clhs] = set()
                                        runner.valid_fds[clhs].update(crhs)
                                    else:
                                        rhs_tuple = tuple(crhs) if isinstance(crhs, list) else (crhs,) if not isinstance(crhs, tuple) else crhs
                                        if isinstance(crhs, (list, tuple)):
                                            rhs_str = ' => '.join(str(col) for col in crhs)
                                        else:
                                            rhs_str = str(crhs)
                                        manual_label = f"OD: {clhs} ↦ [{rhs_str}]"
                                        # Store in a separate tracking dict for user-validated ODs
                                        if "user_validated_ods" not in st.session_state:
                                            st.session_state.user_validated_ods = {}
                                        st.session_state.user_validated_ods[manual_label] = (clhs, rhs_tuple)
                                        # Also add to runner for hint generation
                                        if clhs not in runner.valid_ods:
                                            runner.valid_ods[clhs] = set()
                                        runner.valid_ods[clhs].add(rhs_tuple)
                                    # Mark as user validated so it gets the USER VALIDATED badge
                                    st.session_state.manually_added_deps.add(manual_label)
                                    st.success("Dependency validated and marked as USER VALIDATED!")
                                    st.rerun()
            else:
                st.info("No dependencies discovered yet. Enter a query and click 'Discover Dependencies'.")
        
        with select_col:
            st.markdown("#### Selection Summary")
            
            selected_set = st.session_state.get("selected_dependencies_set", set())
            
            if selected_set:
                st.success(f"**{len(selected_set)}** dependencies selected")
                
                for label in selected_set:
                    if label in dep_details:
                        dtype, lhs, rhs = dep_details[label]
                        color_idx = st.session_state.selected_dep_colors.get(label, 0)
                        color = DEPENDENCY_COLORS[color_idx % len(DEPENDENCY_COLORS)]
                        
                        st.markdown(f'<div class="dependency-card" style="border-left-color: {color};"><span class="dep-badge {"fd-badge" if dtype == "FD" else "od-badge"}">{dtype}</span><br><small><strong>{lhs}</strong></small></div>', unsafe_allow_html=True)
                
                st.markdown("#### Hint Preview")
                hint_entries = []
                for label in selected_set:
                    if label in dep_details:
                        dtype, lhs, rhs = dep_details[label]
                        hint_entries.append(format_dependency_hint(dtype, lhs, rhs))
                
                if hint_entries:
                    hint_json = f"[{', '.join(hint_entries)}]"
                    st.code(hint_json, language="json")
            else:
                st.info("Select dependencies from the list to see preview")
            
            st.markdown("---")
            
            apply_enabled = bool(selected_set) and st.session_state.query_input.current.strip()
            
            if st.button("Apply Rewrite", disabled=not apply_enabled, type="primary", use_container_width=True):
                base_query = st.session_state.query_input.current.strip()
                
                hint_entries = []
                for label in selected_set:
                    if label in dep_details:
                        dtype, lhs, rhs = dep_details[label]
                        hint_entries.append(format_dependency_hint(dtype, lhs, rhs))
                
                rewritten = apply_rewrite_to_query(base_query, hint_entries)
                rewritten = remove_duplicate_hints(rewritten)
                
                st.session_state.rewritten_query = rewritten
                st.session_state.workflow_state = "rewrite"
                
                # IMPORTANT: Save a SNAPSHOT of the colors and selected deps at rewrite time
                # These are the colors that will be used to display the rewritten query
                st.session_state.rewritten_dep_colors = dict(st.session_state.selected_dep_colors)
                st.session_state.rewritten_dependencies_set = set(selected_set)
                
                if rewritten:
                    st.success("Rewritten query generated!")
                else:
                    st.warning("Rewritten query is empty.")
                
                st.rerun()
            
            if st.button("Clear Selection", disabled=not selected_set, use_container_width=True):
                st.session_state.selected_dependencies_set = set()
                st.session_state.selected_dep_colors = {}
                st.session_state.rewritten_query = ""
                st.session_state.rewritten_plan = None
                st.rerun()
    else:
        st.info("Enter a query and click 'Discover Dependencies' to begin the workflow.")

    # ==================== STEP 3: Query Plan Comparison ====================
    st.markdown("---")
    st.markdown('<div class="step-header"><div class="step-number">3</div><div class="step-title">Query Plan Comparison</div></div>', unsafe_allow_html=True)
    
    # Helper function to select the right rewritten plan based on selected dependencies
    def get_demo_rewritten_plan(demo_entry, selected_deps):
        """Select the appropriate rewritten plan based on selected dependencies."""
        rewritten_plans = demo_entry.get('rewritten_plans', {})
        
        if not rewritten_plans:
            # Fallback to single rewritten_plan if no variants
            return demo_entry.get('rewritten_plan', demo_entry.get('original_plan', {}))
        
        # Check which relevant dependencies are selected
        has_od_date = False
        has_fd_cs_order = False
        has_od_cs_order = False
        
        for dep_label in selected_deps:
            # Check for OD on date_dim (d_date_sk ↦ [d_year, d_moy] or similar)
            if "OD:" in dep_label and "date_dim" in dep_label and "d_date_sk" in dep_label:
                has_od_date = True
            # Check for FD on catalog_sales (cs_order_number -> ...)
            if "FD:" in dep_label and "catalog_sales" in dep_label and "cs_order_number" in dep_label:
                has_fd_cs_order = True
            # Check for OD on catalog_sales (cs_order_number ↦ [cs_sold_date_sk])
            if "OD:" in dep_label and "catalog_sales" in dep_label and "cs_order_number" in dep_label:
                has_od_cs_order = True
        
        # Select the appropriate plan variant
        if has_od_date and has_fd_cs_order and has_od_cs_order:
            plan_key = "all"
        elif has_od_date and has_fd_cs_order:
            plan_key = "od_date_fd_cs"
        elif has_od_date and has_od_cs_order:
            plan_key = "all"  # OD date + OD cs_order is close to "all"
        elif has_fd_cs_order and has_od_cs_order:
            plan_key = "od_cs_order"  # OD cs_order dominates
        elif has_od_date:
            plan_key = "od_date"
        elif has_fd_cs_order:
            plan_key = "fd_cs_order"
        elif has_od_cs_order:
            plan_key = "od_cs_order"
        else:
            plan_key = "none"
        
        return rewritten_plans.get(plan_key, rewritten_plans.get("none", demo_entry.get('original_plan', {})))
    
    # Auto-load plans in demo mode
    if demo_mode and selected_demo_query:
        demo_entry = demo_queries_dict[selected_demo_query]
        
        # Load original plan button
        if st.button("Load Query Plans", disabled=not st.session_state.has_query, type="primary"):
            # Load original plan
            orig_plan = demo_entry.get('original_plan', {}).copy()
            if orig_plan.get('nodes'):
                orig_plan = copy.deepcopy(orig_plan)
                node_map = {node['id']: node for node in orig_plan['nodes']}
                for node in orig_plan['nodes']:
                    node['children'] = []
                root_nodes = []
                for node in orig_plan['nodes']:
                    if node['parent_id'] is None or node['parent_id'] not in node_map:
                        root_nodes.append(node)
                    else:
                        node_map[node['parent_id']]['children'].append(node)
                orig_plan['root'] = root_nodes
                st.session_state.original_plan = orig_plan
            
            # Load rewritten plan if query was rewritten - SELECT BASED ON DEPENDENCIES
            if st.session_state.get("rewritten_query"):
                selected_deps = st.session_state.get("selected_dependencies_set", set())
                rewr_plan_template = get_demo_rewritten_plan(demo_entry, selected_deps)
                
                if rewr_plan_template and rewr_plan_template.get('nodes'):
                    rewr_plan = copy.deepcopy(rewr_plan_template)
                    node_map = {node['id']: node for node in rewr_plan['nodes']}
                    for node in rewr_plan['nodes']:
                        node['children'] = []
                    root_nodes = []
                    for node in rewr_plan['nodes']:
                        if node['parent_id'] is None or node['parent_id'] not in node_map:
                            root_nodes.append(node)
                        else:
                            node_map[node['parent_id']]['children'].append(node)
                    rewr_plan['root'] = root_nodes
                    st.session_state.rewritten_plan = rewr_plan
            
            st.success("Plans loaded!")
            st.rerun()
    elif not demo_mode:
        if st.button("Generate Query Plans", disabled=not (st.session_state.connected and st.session_state.has_query)):
            with st.spinner("Generating explain plans..."):
                try:
                    plan_data = parse_explain_plan(runner, st.session_state.query_input.current)
                    st.session_state.original_plan = plan_data
                    
                    if st.session_state.get("rewritten_query"):
                        rewr_plan_data = parse_explain_plan(runner, st.session_state.rewritten_query)
                        st.session_state.rewritten_plan = rewr_plan_data
                    
                    st.success("Plans generated!")
                except Exception as e:
                    st.error(f"Failed to generate plans: {str(e)}")
    
    # Show comparison view (primary view)
    # IMPORTANT: Only show comparison if BOTH plans exist AND there are selected dependencies
    has_selected_deps = bool(st.session_state.get("selected_dependencies_set"))
    has_rewritten_query = bool(st.session_state.get("rewritten_query", "").strip())
    has_rewritten_plan = st.session_state.get("rewritten_plan") is not None
    
    if st.session_state.get("original_plan") and has_rewritten_plan and has_rewritten_query and has_selected_deps:
        orig_plan = st.session_state.original_plan
        rewr_plan = st.session_state.rewritten_plan
        
        # Comparison visualization
        comparison_dot = generate_comparison_graphviz(orig_plan['nodes'], rewr_plan['nodes'])
        st.graphviz_chart(comparison_dot, use_container_width=False)
    elif st.session_state.get("original_plan"):
        st.markdown("#### Original Query Plan")
        orig_plan = st.session_state.original_plan
        if orig_plan.get('nodes'):
            dot_code = generate_graphviz_dot(orig_plan['nodes'], "Original Query Plan", show_details=True)
            st.graphviz_chart(dot_code, use_container_width=False)
        if not has_selected_deps:
            st.info("Select dependencies and apply rewrite to see the comparison view")
        else:
            st.info("Apply a rewrite and load plans to see the comparison view")
    else:
        st.info("Load query plans to see the comparison visualization")

    # ==================== STEP 4: Performance Comparison ====================
    st.markdown("---")
    st.markdown('<div class="step-header"><div class="step-number">4</div><div class="step-title">Performance Comparison</div></div>', unsafe_allow_html=True)
    
    # Initialize benchmark state
    if "benchmark_running" not in st.session_state:
        st.session_state.benchmark_running = False
        st.session_state.benchmark_stop = False
        st.session_state.current_original_times = []
        st.session_state.current_rewritten_times = []
    
    can_compare = st.session_state.has_query and bool(st.session_state.get("rewritten_query"))
    
    # Start/Stop buttons
    btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 2])
    
    with btn_col1:
        if not st.session_state.benchmark_running:
            if st.button("START", disabled=not can_compare, type="primary", use_container_width=True):
                st.session_state.benchmark_running = True
                st.session_state.benchmark_stop = False
                st.session_state.current_original_times = []
                st.session_state.current_rewritten_times = []
                st.rerun()
        else:
            st.button("Running...", disabled=True, use_container_width=True)
    
    with btn_col2:
        if st.session_state.benchmark_running:
            if st.button("STOP", type="secondary", use_container_width=True):
                st.session_state.benchmark_stop = True
                st.session_state.benchmark_running = False
                # Save to history when stopping
                if st.session_state.current_original_times and st.session_state.current_rewritten_times:
                    orig_times = st.session_state.current_original_times
                    rewr_times = st.session_state.current_rewritten_times
                    orig_avg = sum(orig_times) / len(orig_times)
                    rewr_avg = sum(rewr_times) / len(rewr_times)
                    orig_std = (sum((x - orig_avg)**2 for x in orig_times) / len(orig_times)) ** 0.5
                    rewr_std = (sum((x - rewr_avg)**2 for x in rewr_times) / len(rewr_times)) ** 0.5
                    
                    history_entry = {
                        'timestamp': time.strftime("%H:%M:%S"),
                        'original_query': st.session_state.query_input.current,
                        'rewritten_query': st.session_state.rewritten_query,
                        'original_avg': orig_avg,
                        'original_std': orig_std,
                        'rewritten_avg': rewr_avg,
                        'rewritten_std': rewr_std,
                        'num_runs': len(orig_times),
                        'selected_deps': list(st.session_state.get("selected_dependencies_set", set()))
                    }
                    st.session_state.performance_history.append(history_entry)
                st.success("Benchmark saved to history!")
                st.rerun()
        else:
            st.button("STOP", disabled=True, use_container_width=True)
    
    with btn_col3:
        if st.session_state.benchmark_running:
            run_count = len(st.session_state.current_original_times)
            st.info(f"Running benchmark... ({run_count} iterations completed)")
    
    # Live chart during benchmark
    if st.session_state.current_original_times or st.session_state.current_rewritten_times:
        st.markdown("#### Live Benchmark Results")
        
        orig_times = st.session_state.current_original_times
        rewr_times = st.session_state.current_rewritten_times
        
        if orig_times and rewr_times:
            # Calculate statistics
            orig_avg = sum(orig_times) / len(orig_times)
            rewr_avg = sum(rewr_times) / len(rewr_times)
            orig_std = (sum((x - orig_avg)**2 for x in orig_times) / len(orig_times)) ** 0.5 if len(orig_times) > 1 else 0
            rewr_std = (sum((x - rewr_avg)**2 for x in rewr_times) / len(rewr_times)) ** 0.5 if len(rewr_times) > 1 else 0
            
            # Bar chart with current data
            bar_data = pd.DataFrame({
                'Query': ['Original', 'Rewritten'],
                'Avg Time (s)': [orig_avg, rewr_avg],
                'Std': [orig_std, rewr_std]
            })
            
            improvement = ((orig_avg - rewr_avg) / orig_avg) * 100 if orig_avg > 0 else 0
            
            # Bar chart
            bars = alt.Chart(bar_data).mark_bar().encode(
                x=alt.X('Query:N', axis=alt.Axis(labelAngle=0)),
                y=alt.Y('Avg Time (s):Q', title='Execution Time (s)'),
                color=alt.Color('Query:N', scale=alt.Scale(domain=['Original', 'Rewritten'], range=[BAR_COLORS['Original'], BAR_COLORS['Rewritten']]), legend=None)
            )
            
            # Error bars
            error_bars = alt.Chart(bar_data).mark_errorbar(extent='stdev').encode(
                x=alt.X('Query:N'),
                y=alt.Y('Avg Time (s):Q'),
                yError=alt.YError('Std:Q')
            )
            
            chart = (bars + error_bars).properties(
                height=250,
                title=f"Current Benchmark ({len(orig_times)} runs)"
            )
            
            st.altair_chart(chart, use_container_width=True)
            
            # Stats with stdv prominently shown
            stat_cols = st.columns(5)
            with stat_cols[0]:
                st.metric("Original Avg", f"{orig_avg:.3f}s")
            with stat_cols[1]:
                st.metric("Original StdDev", f"+/-{orig_std:.3f}s")
            with stat_cols[2]:
                st.metric("Rewritten Avg", f"{rewr_avg:.3f}s")
            with stat_cols[3]:
                st.metric("Rewritten StdDev", f"+/-{rewr_std:.3f}s")
            with stat_cols[4]:
                time_diff = orig_avg - rewr_avg
                st.metric("Avg Time Saved", f"{time_diff:.3f}s", delta=f"{time_diff:.3f}s")
            
            # Improvement metric
            if improvement > 0:
                st.success(f"**{improvement:.1f}% improvement** ({time_diff:.3f}s faster) with dependency hints! (based on {len(orig_times)} runs)")
            elif improvement < 0:
                st.warning(f"Rewritten query is {abs(improvement):.1f}% slower ({abs(time_diff):.3f}s)")
    
    # Handle continuous execution
    if st.session_state.benchmark_running and not st.session_state.benchmark_stop:
        if demo_mode and selected_demo_query:
            demo_entry = demo_queries_dict[selected_demo_query]
            perf_data = demo_entry.get("demo_performance", {})
            
            base_orig = perf_data.get('original_avg', 1.5)
            base_rewr = perf_data.get('rewritten_avg', 0.8)
            std_orig = perf_data.get('original_std', 0.3)
            std_rewr = perf_data.get('rewritten_std', 0.1)
            
            # Simulate one run
            time.sleep(0.3)  # Small delay for visual feedback
            st.session_state.current_original_times.append(random.gauss(base_orig, std_orig))
            st.session_state.current_rewritten_times.append(random.gauss(base_rewr, std_rewr))
            st.rerun()
        elif st.session_state.connected:
            # Real execution
            orig_result = execute_query_with_timing(runner, st.session_state.query_input.current, False)
            rewr_result = execute_query_with_timing(runner, st.session_state.rewritten_query, False)
            
            if 'error' not in orig_result:
                st.session_state.current_original_times.append(orig_result['execution_time'])
            if 'error' not in rewr_result:
                st.session_state.current_rewritten_times.append(rewr_result['execution_time'])
            st.rerun()
    
    # Display performance history as bar charts
    if st.session_state.get("performance_history"):
        history_header_col1, history_header_col2 = st.columns([3, 1])
        with history_header_col1:
            st.markdown("#### Benchmark History")
        with history_header_col2:
            if st.button("Clear History", key="clear_history_btn", use_container_width=True):
                st.session_state.performance_history = []
                st.rerun()
        
        for idx, entry in enumerate(reversed(st.session_state.performance_history)):
            real_idx = len(st.session_state.performance_history) - 1 - idx
            
            col1, col2 = st.columns([3, 1])
            
            with col1:
                # Create bar chart with error bars
                bar_data = pd.DataFrame({
                    'Query': ['Original', 'Rewritten'],
                    'Avg Time (s)': [entry['original_avg'], entry['rewritten_avg']],
                    'Std': [entry['original_std'], entry['rewritten_std']]
                })
                
                # Calculate improvement
                improvement = ((entry['original_avg'] - entry['rewritten_avg']) / entry['original_avg']) * 100
                
                # Bar chart
                bars = alt.Chart(bar_data).mark_bar().encode(
                    x=alt.X('Query:N', axis=alt.Axis(labelAngle=0)),
                    y=alt.Y('Avg Time (s):Q', title='Execution Time (s)'),
                    color=alt.Color('Query:N', scale=alt.Scale(domain=['Original', 'Rewritten'], range=[BAR_COLORS['Original'], BAR_COLORS['Rewritten']]), legend=None)
                )
                
                # Error bars
                error_bars = alt.Chart(bar_data).mark_errorbar(extent='stdev').encode(
                    x=alt.X('Query:N'),
                    y=alt.Y('Avg Time (s):Q'),
                    yError=alt.YError('Std:Q')
                )
                
                chart = (bars + error_bars).properties(
                    height=200,
                    title=f"Benchmark #{real_idx + 1} @ {entry['timestamp']}"
                )
                
                st.altair_chart(chart, use_container_width=True)
                
                # Stats below chart
                stat_cols = st.columns(3)
                with stat_cols[0]:
                    st.metric("Original Avg", f"{entry['original_avg']:.3f}s", help=f"Std: +/-{entry['original_std']:.3f}s")
                with stat_cols[1]:
                    st.metric("Rewritten Avg", f"{entry['rewritten_avg']:.3f}s", help=f"Std: +/-{entry['rewritten_std']:.3f}s")
                with stat_cols[2]:
                    st.metric("Improvement", f"{improvement:.1f}%", delta=f"{improvement:.1f}%")
            
            with col2:
                # Button to show query details in expander
                with st.expander("View Queries"):
                    st.markdown("**Original Query:**")
                    st.code(entry['original_query'], language="sql")
                    st.markdown("**Rewritten Query:**")
                    st.code(entry['rewritten_query'], language="sql")
                    st.markdown("**Selected Dependencies:**")
                    for dep in entry.get('selected_deps', []):
                        st.write(f"- {dep[:60]}...")
            
            if idx < len(st.session_state.performance_history) - 1:
                st.markdown("---")
    else:
        st.info("Run a benchmark to see performance comparison")

    # ==================== Query Results Toggle ====================
    st.markdown("---")
    st.markdown('<div class="step-header"><div class="step-number">5</div><div class="step-title">Query Results Preview</div></div>', unsafe_allow_html=True)
    
    show_results = st.toggle("Show Query Results (First 100 rows)", value=st.session_state.get("show_query_results", False))
    st.session_state.show_query_results = show_results
    
    if show_results:
        if st.session_state.connected and st.session_state.has_query and st.session_state.get("rewritten_query"):
            # Real database - fetch actual results
            if st.button("Fetch Query Results", type="primary"):
                with st.spinner("Executing queries..."):
                    try:
                        # Execute original query
                        add_sql_log(st.session_state.query_input.current, "QUERY")
                        runner.cursor.execute(st.session_state.query_input.current)
                        original_results = runner.cursor.fetchall()
                        original_columns = [desc[0] for desc in runner.cursor.description] if runner.cursor.description else []
                        
                        # Execute rewritten query
                        add_sql_log(st.session_state.rewritten_query, "QUERY")
                        runner.cursor.execute(st.session_state.rewritten_query)
                        rewritten_results = runner.cursor.fetchall()
                        rewritten_columns = [desc[0] for desc in runner.cursor.description] if runner.cursor.description else []
                        
                        # Store results
                        st.session_state.last_query_results = {
                            'original': original_results[:100],
                            'original_cols': original_columns,
                            'rewritten': rewritten_results[:100],
                            'rewritten_cols': rewritten_columns,
                            'total_original': len(original_results),
                            'total_rewritten': len(rewritten_results)
                        }
                        st.success("Results fetched!")
                        st.rerun()
                    except Exception as e:
                        add_sql_log(f"Error: {str(e)}", "ERROR")
                        st.error(f"Error fetching results: {str(e)}")
            
            # Display stored results
            if st.session_state.get("last_query_results", {}).get("original"):
                results = st.session_state.last_query_results
                
                # Check if results match
                results_match = (results['total_original'] == results['total_rewritten'] and 
                               results['original'] == results['rewritten'])
                
                if results_match:
                    st.markdown('''
                    <div style="background: linear-gradient(135deg, #28a745 0%, #20c997 100%); color: white; padding: 15px 20px; border-radius: 8px; margin-bottom: 15px; text-align: center;">
                        <span style="font-size: 18px; font-weight: bold;">RESULTS IDENTICAL</span>
                        <span style="font-size: 14px; display: block; margin-top: 5px; opacity: 0.9;">Query rewrite only affects performance, not results. Both queries return the same data.</span>
                    </div>
                    ''', unsafe_allow_html=True)
                else:
                    st.warning(f"Results differ: Original has {results['total_original']} rows, Rewritten has {results['total_rewritten']} rows")
                
                res_col1, res_col2 = st.columns(2)
                
                with res_col1:
                    st.markdown(f"**Original Query Results:** ({results['total_original']} total rows)")
                    if results['original'] and results['original_cols']:
                        df_orig = pd.DataFrame(results['original'], columns=results['original_cols'])
                        st.dataframe(df_orig, use_container_width=True)
                    else:
                        st.info("No results")
                
                with res_col2:
                    st.markdown(f"**Rewritten Query Results:** ({results['total_rewritten']} total rows)")
                    if results['rewritten'] and results['rewritten_cols']:
                        df_rewr = pd.DataFrame(results['rewritten'], columns=results['rewritten_cols'])
                        st.dataframe(df_rewr, use_container_width=True)
                    else:
                        st.info("No results")
            else:
                st.info("Click 'Fetch Query Results' to execute queries and see results")
        
        elif demo_mode and selected_demo_query:
            # Demo results specific to each query (matching the SELECT columns)
            demo_results_dict = {
                "TPCDS Catalog Returns Complex Join": [
                    # SELECT cr_order_number, cr_item_sk
                    {"cr_order_number": 144523, "cr_item_sk": 8934},
                    {"cr_order_number": 144523, "cr_item_sk": 12456},
                    {"cr_order_number": 167892, "cr_item_sk": 3421},
                    {"cr_order_number": 189034, "cr_item_sk": 7823},
                    {"cr_order_number": 201456, "cr_item_sk": 9012},
                    {"cr_order_number": 201456, "cr_item_sk": 11234},
                    {"cr_order_number": 223789, "cr_item_sk": 5678},
                    {"cr_order_number": 245123, "cr_item_sk": 2345},
                    {"cr_order_number": 267456, "cr_item_sk": 6789},
                    {"cr_order_number": 289012, "cr_item_sk": 4567},
                ],
                "TPCDS Catalog Sales Date Join": [
                    # SELECT cs_sold_date_sk, cs_item_sk
                    {"cs_sold_date_sk": 2451180, "cs_item_sk": 1234},
                    {"cs_sold_date_sk": 2451211, "cs_item_sk": 5678},
                    {"cs_sold_date_sk": 2451241, "cs_item_sk": 9012},
                    {"cs_sold_date_sk": 2451272, "cs_item_sk": 3456},
                    {"cs_sold_date_sk": 2451302, "cs_item_sk": 7890},
                    {"cs_sold_date_sk": 2451333, "cs_item_sk": 2345},
                    {"cs_sold_date_sk": 2451364, "cs_item_sk": 6789},
                    {"cs_sold_date_sk": 2451394, "cs_item_sk": 1011},
                    {"cs_sold_date_sk": 2451425, "cs_item_sk": 1213},
                    {"cs_sold_date_sk": 2451455, "cs_item_sk": 1415},
                ],
                "TPCDS Store Sales Analysis": [
                    # SELECT ss_sold_date_sk, ss_store_sk
                    {"ss_sold_date_sk": 2451911, "ss_store_sk": 101},
                    {"ss_sold_date_sk": 2451942, "ss_store_sk": 102},
                    {"ss_sold_date_sk": 2451972, "ss_store_sk": 103},
                    {"ss_sold_date_sk": 2452003, "ss_store_sk": 104},
                    {"ss_sold_date_sk": 2452033, "ss_store_sk": 105},
                    {"ss_sold_date_sk": 2452064, "ss_store_sk": 106},
                    {"ss_sold_date_sk": 2452095, "ss_store_sk": 107},
                    {"ss_sold_date_sk": 2452125, "ss_store_sk": 108},
                    {"ss_sold_date_sk": 2452156, "ss_store_sk": 109},
                    {"ss_sold_date_sk": 2452186, "ss_store_sk": 110},
                ],
            }
            
            demo_results = demo_results_dict.get(selected_demo_query, [])
            
            # Show prominent "Results Match" banner at the top
            st.markdown('''
            <div style="background: linear-gradient(135deg, #28a745 0%, #20c997 100%); color: white; padding: 15px 20px; border-radius: 8px; margin-bottom: 15px; text-align: center;">
                <span style="font-size: 18px; font-weight: bold;">RESULTS IDENTICAL</span>
                <span style="font-size: 14px; display: block; margin-top: 5px; opacity: 0.9;">Query rewrite only affects performance, not results. Both queries return the same data.</span>
            </div>
            ''', unsafe_allow_html=True)
            
            res_col1, res_col2 = st.columns(2)
            
            with res_col1:
                st.markdown("**Original Query Results:**")
                st.dataframe(pd.DataFrame(demo_results), use_container_width=True)
            
            with res_col2:
                st.markdown("**Rewritten Query Results:**")
                st.dataframe(pd.DataFrame(demo_results), use_container_width=True)
        else:
            st.info("Connect to a database and run queries to see actual results")

    # ==================== STEP 6: SQL Logs ====================
    st.markdown("---")
    st.markdown('<div class="step-header"><div class="step-number">6</div><div class="step-title">SQL Logs</div></div>', unsafe_allow_html=True)
    
    # Initialize SQL logs state
    if "sql_logs" not in st.session_state:
        st.session_state.sql_logs = []
    if "show_sql_logs" not in st.session_state:
        st.session_state.show_sql_logs = False
    
    show_logs = st.toggle("Show SQL Logs", value=st.session_state.get("show_sql_logs", False))
    st.session_state.show_sql_logs = show_logs
    
    if show_logs:
        st.markdown("**SQL Execution Logs** *(showing last 20 entries, older entries are automatically pushed out)*")
        
        if demo_mode:
            # Demo SQL logs
            demo_logs = [
                {"timestamp": "10:05:23", "type": "QUERY", "sql": "EXPLAIN PLAN SET STATEMENT_NAME = 'dep_check_query_123456' FOR SELECT cr_order_number, cr_item_sk..."},
                {"timestamp": "10:05:24", "type": "QUERY", "sql": "SELECT operator_id, parent_operator_id, operator_name, operator_details, table_name FROM SYS.EXPLAIN_PLAN_TABLE WHERE STATEMENT_NAME = 'dep_check_query_123456'"},
                {"timestamp": "10:05:25", "type": "DELETE", "sql": "DELETE FROM SYS.EXPLAIN_PLAN_TABLE WHERE STATEMENT_NAME = 'dep_check_query_123456'"},
                {"timestamp": "10:05:30", "type": "QUERY", "sql": "SELECT cr_order_number, cr_item_sk, cs_sold_date_sk, c_customer_id, ca_city FROM catalog_returns_sanitized..."},
                {"timestamp": "10:05:32", "type": "QUERY", "sql": "SELECT cr_order_number, cr_item_sk, cs_sold_date_sk, c_customer_id, ca_city FROM catalog_returns_sanitized... WITH HINT(DEV_DATA_DEPENDENCIES(...))"},
                {"timestamp": "10:05:35", "type": "BENCHMARK", "sql": "Benchmark iteration 1: Original=2.34s, Rewritten=0.89s"},
                {"timestamp": "10:05:38", "type": "BENCHMARK", "sql": "Benchmark iteration 2: Original=2.21s, Rewritten=0.92s"},
            ]
            
            for log in demo_logs[-20:]:  # Show last 20
                log_type_color = {
                    "QUERY": "#4CAF50",
                    "DELETE": "#FF9800",
                    "BENCHMARK": "#2196F3",
                }.get(log["type"], "#666")
                
                st.markdown(f'''
                <div style="background-color: #f8f9fa; border-left: 4px solid {log_type_color}; padding: 8px 12px; margin: 4px 0; font-family: monospace; font-size: 11px;">
                    <span style="color: #666;">[{log["timestamp"]}]</span>
                    <span style="background-color: {log_type_color}; color: white; padding: 2px 6px; border-radius: 4px; font-size: 10px; margin: 0 8px;">{log["type"]}</span>
                    <code>{log["sql"][:100]}{"..." if len(log["sql"]) > 100 else ""}</code>
                </div>
                ''', unsafe_allow_html=True)
        else:
            if st.session_state.sql_logs:
                for log in st.session_state.sql_logs[-20:]:  # Show last 20 logs (rolling window)
                    log_type_color = {
                        "QUERY": "#4CAF50",
                        "DELETE": "#FF9800",
                        "BENCHMARK": "#2196F3",
                        "ERROR": "#dc3545",
                    }.get(log.get("type", "QUERY"), "#666")
                    
                    st.markdown(f'''
                    <div style="background-color: #f8f9fa; border-left: 4px solid {log_type_color}; padding: 8px 12px; margin: 4px 0; font-family: monospace; font-size: 11px;">
                        <span style="color: #666;">[{log.get("timestamp", "")}]</span>
                        <span style="background-color: {log_type_color}; color: white; padding: 2px 6px; border-radius: 4px; font-size: 10px; margin: 0 8px;">{log.get("type", "QUERY")}</span>
                        <code>{log.get("sql", "")[:100]}{"..." if len(log.get("sql", "")) > 100 else ""}</code>
                    </div>
                    ''', unsafe_allow_html=True)
            else:
                st.info("No SQL logs yet. Execute queries to see the logs.")

    st.write(css, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
