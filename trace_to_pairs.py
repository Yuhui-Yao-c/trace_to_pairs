# -*- coding: utf-8 -*-
"""v9.28 trace → step-pair extraction.

Reuses trace_jump_analyzer.parse_trace() (UTF-16 TwinCAT Scope .trace parser)
to obtain per-variable value/timestamp series, then converts adjacent jumps
into state-transition pairs of the same shape as prediction_collection_pairs.csv
so downstream action_describe.py (A/B-tier) and gen_workstation_xlsx.py can be
reused without modification.

Pair construction (single-variable, per-jump):
  For a variable whose value sequence is  0 → 100 → 110 → 120 → 0,
  jumps = [(0→100,ts0),(100→110,ts1),(110→120,ts2),(120→0,ts3)]
  pairs  = [
    (var=0   → var=100, dur=ts0-first_sample_ts),  # first jump, dwell from sampling start
    (var=100 → var=110, dur=ts1-ts0),
    (var=110 → var=120, dur=ts2-ts1),
    (var=120 → var=0,   dur=ts3-ts2),              # reset exit, kept + flagged
  ]
  Every jump is a pair, including jumps[0] (whose duration uses the variable's
  first-sample timestamp as the dwell start).

Multi-cycle aggregation:
  The same (var, from_val, to_val) triple observed N times across cycles is
  collapsed into ONE row with median/min/max duration and occurrence count,
  per user requirement (median is more robust than mean to outlier cycles).

Outputs (in run dir):
  trace_collection_pairs.csv  — main pair table (column-compatible with
                                prediction_collection_pairs.csv + runtime cols)
  trace_variables.json        — per-variable metadata (reused from analyzer)
  trace_jumps.csv             — all raw jumps (audit)
  trace_segments.csv          — all value segments (audit)
  trace_cycles.json           — cycle boundary timestamps (audit)
  trace_pairs_review.csv      — pairs whose variable is absent from source
                                candidate pool (no action-variable mapping)
  trace_pairs_summary.json    — run-level stats

Isolation: reads only the trace file + source-derived evidence files in run
(plcopen_action_trace.csv, step_comments.json, station_identity.json,
variable_documentation.json). Never opens GT or manual tables.
"""
import csv
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

# Reuse the proven trace parser — it already handles UTF-16 + <TraceVariable>.
from trace_jump_analyzer import parse_trace, detect_cycles, _write_csv
# v9.28: shared utilities (eliminates duplication with gen_stepvars_xlsx).
from utils import load_station_identity, infer_proc_name, em_sort_key_prefixed


# State-variable symbol regex (same shape as gen_stepvars_xlsx._SYMBOL_RE).
# Matches  var[idx]  in labels like  EM06V.Process_steps[1]=100
_SYMBOL_RE = re.compile(r'([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\[\d+\])')


def _label(var, val):
    """Format a state label  var=value  with int coercion when possible."""
    if val == int(val):
        val = int(val)
    return f'{var}={val}'


def _var_root(var_name):
    """Extract the instance root for station lookup.

    EM06V.Process_steps[1] -> EM06V
    udt_EM01.adiAutoStep[5] -> udt_EM01
    """
    m = re.match(r'^([A-Za-z_]\w*)', var_name)
    return m.group(1) if m else var_name


def _val_to_int(v):
    """Coerce float to int when lossless (trace values are float from parser)."""
    try:
        if v == int(v):
            return int(v)
    except (TypeError, ValueError, OverflowError):
        pass
    return v


def _load_step_comments(run):
    """Load step_comments.json (start_label -> Chinese comment)."""
    p = os.path.join(run, 'step_comments.json')
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding='utf-8'))
        except Exception:
            pass
    return {}


def _load_var_docs(run):
    """Load variable_documentation.json (root -> Chinese doc)."""
    p = os.path.join(run, 'variable_documentation.json')
    if not os.path.exists(p):
        return {}
    try:
        data = json.load(open(p, encoding='utf-8'))
        st = data.get('stations') or {}
        src = st if st else data.get('all', {})
        out = {}
        for k, v in src.items():
            t = re.sub(r'<[^>]+>', '', str(v)).strip()
            if t:
                out[k] = t
        return out
    except Exception:
        return {}


def _load_action_map(run):
    """(state_variable, state_value) -> [action_variable, ...].

    Reuses gen_workstation_xlsx.load_action_map to stay consistent with the
    prediction-side action-variable mapping (plcopen_action_trace.csv +
    native_command_evidence.csv + indexed_events.csv ST_assignment).
    """
    try:
        from gen_workstation_xlsx import load_action_map
        return load_action_map(run)
    except Exception:
        return {}


def _station_name(var_name, si_map, var_docs):
    """Resolve display station name for a trace variable."""
    root = _var_root(var_name)
    si = si_map.get(root)
    if si and si.get('display'):
        return si['display']
    doc = var_docs.get(root)
    if doc:
        return f'{root}_{doc}'
    return root


# Column schema for trace_collection_pairs.csv.
# Includes the CANDIDATE_FIELDS subset that gen_workstation_xlsx.generate()
# and action_describe.cmd_a() actually read, plus runtime columns.
PAIR_FIELDS = [
    '构成要素', '动作', '动作描述', '开始标签', '结束标签', '状态',
    '证据POU', '证据节点', '原始条件', '控制器', '分支数',
    # Runtime columns (trace-specific):
    '动作变量', '发生次数', '中位数时长ms', '最小时长ms', '最大时长ms',
    '首次时间ms', '末次时间ms', '所属周期数', '备注',
]


def generate(trace_path, run, source=None, proc_name=None,
             idle_threshold_ms=5000):
    """Run the full trace → pairs extraction.

    Parameters
    ----------
    trace_path : str
        Path to the .trace file (TwinCAT Scope, UTF-16 XML).
    run : str
        Run output directory (must already contain source-derived evidence
        files from a prior workflow.create() call: plcopen_action_trace.csv,
        step_comments.json, station_identity.json, variable_documentation.json).
    source : str, optional
        Source XML path (for process-name inference only).
    proc_name : str, optional
        Override process name.
    idle_threshold_ms : int
        Idle-segment threshold for cycle detection (ms).

    Returns
    -------
    dict with run stats and output paths.
    """
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)

    # ---- 1. Parse trace (reuse trace_jump_analyzer) ----
    variables = parse_trace(trace_path)
    active_count = sum(1 for v in variables.values() if not v['inactive'])
    inactive_count = sum(1 for v in variables.values() if v['inactive'])

    # ---- 2. Write trace metadata (audit, same as trace_jump_analyzer) ----
    var_meta = {}
    for name, vd in variables.items():
        var_meta[name] = {
            'sample_count': vd['sample_count'],
            'time_range_ms': [int(vd['time_range'][0]), int(vd['time_range'][1])],
            'inactive': vd['inactive'],
            'jump_count': len(vd['jumps']),
            'unique_values': [_val_to_int(x) for x in vd['unique_values']],
        }
    (run / 'trace_variables.json').write_text(
        json.dumps(var_meta, ensure_ascii=False, indent=2), encoding='utf-8')

    # ---- 3. Write raw jumps + segments (audit) ----
    jump_fields = ['变量', 'from值', 'to值', '时间戳ms', '样本索引']
    jump_rows = []
    for name, vd in variables.items():
        for idx, from_v, to_v, ts in vd['jumps']:
            jump_rows.append({
                '变量': name,
                'from值': _val_to_int(from_v),
                'to值': _val_to_int(to_v),
                '时间戳ms': int(ts),
                '样本索引': idx,
            })
    _write_csv(run / 'trace_jumps.csv', jump_rows, jump_fields)

    seg_fields = ['变量', '值', '起始ms', '结束ms', '时长ms']
    seg_rows = []
    for name, vd in variables.items():
        for value, s_ts, e_ts, dur in vd['segments']:
            seg_rows.append({
                '变量': name,
                '值': _val_to_int(value),
                '起始ms': int(s_ts),
                '结束ms': int(e_ts),
                '时长ms': int(dur),
            })
    _write_csv(run / 'trace_segments.csv', seg_rows, seg_fields)

    # ---- 4. Detect cycles ----
    cycles = detect_cycles(variables, idle_threshold_ms=idle_threshold_ms)
    cycle_data = [{'boundary_ts': int(ts), 'var': name} for ts, name in cycles]
    (run / 'trace_cycles.json').write_text(
        json.dumps(cycle_data, ensure_ascii=False, indent=2), encoding='utf-8')

    # Assign each jump to a cycle index (0 = before first boundary, etc.).
    # A jump at timestamp ts belongs to cycle i if boundary[i-1] <= ts < boundary[i].
    boundaries = [c['boundary_ts'] for c in cycle_data]

    def _cycle_of(ts):
        lo, hi = 0, len(boundaries)
        while lo < hi:
            mid = (lo + hi) // 2
            if boundaries[mid] <= ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    # ---- 5. Build adjacent-jump pairs per variable ----
    # pair_key = (var_name, from_val, to_val) -> list of duration_ms
    pair_durations = defaultdict(list)
    pair_first_ts = {}
    pair_last_ts = {}
    pair_cycles = defaultdict(set)

    for name, vd in variables.items():
        if vd['inactive']:
            continue
        jumps = vd['jumps']  # [(idx, from_v, to_v, ts_ms), ...]
        # Each jump is itself a complete state transition (from_v -> to_v).
        # Iterate from k=0 so the FIRST jump of every variable is captured.
        # (A prior range(1,...) loop silently dropped jumps[0]; when a variable
        # only ran one cycle, its first transition was lost forever — e.g.
        # ST06.AutoStep[2] 50->60, ST07.AutoStep[1] 45->50.)
        first_sample_ts = int(vd['time_range'][0])
        for k in range(len(jumps)):
            _, from_v, to_v, ts = jumps[k]
            # Skip degenerate self-loops (from==to).  These arise when a
            # variable briefly flickers and returns to the same value within
            # one sample period — they are not real state transitions.
            if from_v == to_v:
                continue
            # Duration = how long the variable stayed at from_v before
            # moving to to_v.  For k=0 there is no previous jump, so use
            # the variable's first-sample timestamp (initial state dwell
            # time from sampling start to first transition).
            prev_ts = first_sample_ts if k == 0 else jumps[k - 1][3]
            dur = ts - prev_ts
            key = (name, from_v, to_v)
            pair_durations[key].append(int(dur))
            if key not in pair_first_ts:
                pair_first_ts[key] = int(prev_ts)
            pair_last_ts[key] = int(ts)
            pair_cycles[key].add(_cycle_of(ts))

    # ---- 6. Load source-derived evidence (action map, step comments, identity) ----
    amap = _load_action_map(str(run))
    sc_map = _load_step_comments(str(run))
    si_map = load_station_identity(str(run))
    var_docs = _load_var_docs(str(run))

    # Track which trace variables have action-variable mappings (for review).
    mapped_vars = set()
    for (sv, _val) in amap.keys():
        mapped_vars.add(sv)

    # ---- 7. Assemble pair rows ----
    rows = []
    review_rows = []
    for (var_name, from_v, to_v), durs in pair_durations.items():
        from_int = _val_to_int(from_v)
        to_int = _val_to_int(to_v)
        start_label = _label(var_name, from_int)
        end_label = _label(var_name, to_int)
        is_reset = (to_int == 0)

        # Action variables: (state_variable, state_value) -> [syms]
        # Key uses the bare state variable (no [idx]) to match amap keys
        # which come from plcopen_action_trace.csv state_variable column.
        sv_key = var_name  # amap keys are like 'EM06V.Process_steps[1]'
        action_vars = amap.get((sv_key, str(from_int)), [])
        if not action_vars:
            # Try without index (some action traces use bare array var)
            bare = re.sub(r'\[\d+\]', '', var_name)
            action_vars = amap.get((bare, str(from_int)), [])

        # The measured dwell and its step comment belong to the start state.
        sc = sc_map.get(start_label, '')

        # Station name for grouping
        ws_name = _station_name(var_name, si_map, var_docs)

        median_dur = int(round(statistics.median(durs)))
        min_dur = int(min(durs))
        max_dur = int(max(durs))
        n_cycles = len(pair_cycles[(var_name, from_v, to_v)])

        remark = []
        if is_reset:
            remark.append('复位退出')
        has_action_vars = bool(action_vars)
        if var_name not in mapped_vars:
            remark.append('源码无动作变量映射')
        elif not has_action_vars:
            remark.append('该步值无动作变量')
        if not sc and not has_action_vars:
            remark.append('无步注释')

        rows.append({
            '构成要素': ws_name,
            '动作': 'trace状态跳变',
            '动作描述': sc,
            '开始标签': start_label,
            '结束标签': end_label,
            '状态': 'trace_state_jump',
            '证据POU': '',
            '证据节点': '',
            '原始条件': '',
            '控制器': var_name,
            '分支数': 0,
            '动作变量': '; '.join(action_vars),
            '发生次数': len(durs),
            '中位数时长ms': median_dur,
            '最小时长ms': min_dur,
            '最大时长ms': max_dur,
            '首次时间ms': pair_first_ts[(var_name, from_v, to_v)],
            '末次时间ms': pair_last_ts[(var_name, from_v, to_v)],
            '所属周期数': n_cycles,
            '备注': ' | '.join(remark),
        })

        if var_name not in mapped_vars:
            review_rows.append({
                '类型': 'no_action_mapping',
                '变量': var_name,
                '开始标签': start_label,
                '结束标签': end_label,
                '说明': f'trace变量 {var_name} 不在源码动作变量映射中',
            })
        elif not has_action_vars:
            review_rows.append({
                '类型': 'no_action_for_step_value',
                '变量': var_name,
                '开始标签': start_label,
                '结束标签': end_label,
                '说明': f'{var_name}={from_int} 在源码动作变量映射中无对应动作',
            })

    # Sort: by station sort key, then variable, then from-value
    rows.sort(key=lambda r: (em_sort_key_prefixed(r['构成要素']),
                              em_sort_key_prefixed(r['控制器']),
                              r['开始标签']))

    # ---- 8. Write outputs ----
    csv_path = run / 'trace_collection_pairs.csv'
    with csv_path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=PAIR_FIELDS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)

    rev_fields = ['类型', '变量', '开始标签', '结束标签', '说明']
    _write_csv(run / 'trace_pairs_review.csv', review_rows, rev_fields)

    proc = infer_proc_name(source, str(run), proc_name)

    summary = {
        'trace_path': str(trace_path),
        'run': str(run),
        'proc_name': proc,
        'variables_total': len(variables),
        'variables_active': active_count,
        'variables_inactive': inactive_count,
        'total_jumps': len(jump_rows),
        'total_segments': len(seg_rows),
        'cycles_detected': len(cycle_data),
        'unique_pairs': len(rows),
        'pairs_with_action_vars': sum(1 for r in rows if r['动作变量']),
        'pairs_with_step_comment': sum(1 for r in rows if r['动作描述']),
        'pairs_reset_exit': sum(1 for r in rows if '复位退出' in r['备注']),
        'review_count': len(review_rows),
        'outputs': {
            'pairs_csv': str(csv_path),
            'variables': str(run / 'trace_variables.json'),
            'jumps': str(run / 'trace_jumps.csv'),
            'segments': str(run / 'trace_segments.csv'),
            'cycles': str(run / 'trace_cycles.json'),
            'review': str(run / 'trace_pairs_review.csv'),
        },
    }
    (run / 'trace_pairs_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    return summary


def load_trace_duration_map(run):
    """Load trace_collection_pairs.csv duration column for xlsx backfill.

    Returns dict: (start_label, end_label) -> median_duration_ms (int or None).
    Returns {} when file doesn't exist (not a trace-mode run).
    """
    p = Path(run) / 'trace_collection_pairs.csv'
    if not p.exists():
        return {}
    rt = {}
    with open(p, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            dur = (row.get('中位数时长ms') or '').strip()
            key = (row.get('开始标签', ''), row.get('结束标签', ''))
            try:
                rt[key] = int(dur) if dur else None
            except ValueError:
                rt[key] = None
    return rt


def main():
    import argparse
    ap = argparse.ArgumentParser(description='trace → step pairs extraction')
    ap.add_argument('--trace', required=True, help='.trace 文件路径')
    ap.add_argument('--run', required=True, help='run目录（含源码证据文件）')
    ap.add_argument('--source', default=None, help='源码XML（推断工序名用）')
    ap.add_argument('--proc-name', default=None, help='工序名')
    ap.add_argument('--idle-threshold', type=int, default=5000,
                    help='周期检测空闲阈值(ms)，默认5000')
    a = ap.parse_args()
    stats = generate(a.trace, a.run, a.source, a.proc_name,
                     idle_threshold_ms=a.idle_threshold)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
