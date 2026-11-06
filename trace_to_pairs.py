# -*- coding: utf-8 -*-
"""v9.28 trace → step-pair extraction.

Reads TwinCAT Scope .trace XML via trace_jump_analyzer, or Sysmac Studio trace
tables and recording directories. Converts adjacent observed jumps into state-transition
pairs of the same shape as prediction_collection_pairs.csv
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
  trace_pairs_review.csv      — pairs missing a source action mapping for the
                                variable or the start step
  trace_pairs_summary.json    — run-level stats

Isolation: reads only the trace input + source-derived evidence files in run
(plcopen_action_trace.csv, step_comments.json, station_identity.json,
variable_documentation.json). Never opens GT or manual tables.
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import json
import os
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


# ---- Sysmac trace tables and recording directories ----

TABLE_SUFFIXES = {'.csv', '.tsv', '.txt'}
INTEGER_TYPES = {
    'SINT', 'INT', 'DINT', 'LINT', 'USINT', 'UINT', 'UDINT', 'ULINT',
    'BYTE', 'WORD', 'DWORD', 'LWORD',
}
NS_PER_MS = 1_000_000
CAPTURE_SETTINGS = ('tracetype', 'sampletimingtype', 'taskname', 'samplinginterval', 'samplerate')


@dataclass(frozen=True)
class TablePart:
    path: Path
    encoding: str
    delimiter: str
    channels: dict
    settings: tuple
    capture_start: Decimal | None
    first_index: int
    last_index: int
    row_count: int
    raw_range: tuple | None
    interval_ns: int | None


def _table_format(path):
    with path.open('rb') as stream:
        prefix = stream.read(8192)
    if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = 'utf-16'
    else:
        try:
            codecs.getincrementaldecoder('utf-8-sig')().decode(prefix)
            encoding = 'utf-8-sig'
        except UnicodeDecodeError:
            encoding = 'gb18030'
    for line in prefix.decode(encoding, errors='ignore').splitlines():
        for delimiter in ('\t', ','):
            row = next(csv.reader([line], delimiter=delimiter))
            fields = {cell.strip().casefold() for cell in row}
            if len(row) > 1 and (row[0].strip().casefold() == 'tracetype'
                                 or {'index', 'rawtime'} <= fields):
                return encoding, delimiter
    return None


def is_omron_trace_input(path):
    """Recognize a trace directory or a Sysmac table passed as a single file."""
    path = Path(path)
    return path.is_dir() or (path.is_file() and (
        path.suffix.lower() in {'.csv', '.tsv'}
        or (path.suffix.lower() == '.txt' and _table_format(path) is not None)
    ))


def _find_tables(path):
    paths = sorted(path.iterdir()) if path.is_dir() else [path]
    tables, ignored = [], []
    for candidate in paths:
        if candidate.suffix.lower() not in TABLE_SUFFIXES or not candidate.is_file():
            continue
        fmt = _table_format(candidate)
        if fmt is None:
            ignored.append(candidate.name)
        else:
            tables.append((candidate, fmt))
    if not tables:
        raise ValueError(f'{path}: 找不到含 Index 和 RawTime 的 Sysmac Trace 表（CSV/TSV/TXT）')
    return tables, ignored


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def trace_input_digest(path):
    """Keep the legacy file digest and bind a directory to its Trace files."""
    path = Path(path)
    if not path.is_dir():
        return _file_digest(path)
    tables, _ = _find_tables(path)
    digest = hashlib.sha256()
    for table, _ in tables:
        digest.update(table.name.encode('utf-8') + b'\0')
        digest.update(bytes.fromhex(_file_digest(table)))
    return digest.hexdigest()


def _read_header(reader, path):
    metadata = {}
    for row in reader:
        fields = [cell.strip().casefold() for cell in row]
        if 'index' in fields and 'rawtime' in fields:
            channels = {}
            for column, cell in enumerate(row):
                name, _, data_type = cell.strip().rpartition(':')
                name, data_type = name.strip(), data_type.strip().upper()
                if not name or data_type not in INTEGER_TYPES:
                    continue
                if name in channels:
                    raise ValueError(f'{path}:{reader.line_num}: 重复变量列 {name}')
                channels[name] = (column, data_type)
            return fields.index('index'), fields.index('rawtime'), channels, metadata
        if len(row) > 1 and fields[0]:
            metadata[fields[0]] = row[1].strip()
    raise ValueError(f'{path}: 缺少 Index / RawTime 表头')


def _decimal(text):
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number.adjusted() > 20:
        return None
    return number


def _raw_ns(text):
    if text.isascii() and text.isdecimal():
        return int(text)
    number = _decimal(text)
    if number is None or number.as_tuple().exponent > 6:
        return None
    return int(number)


def _sample_rows(reader, path, index_column, time_column):
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        try:
            index = int(row[index_column])
            raw_time = _raw_ns(row[time_column].strip())
        except (IndexError, ValueError) as exc:
            raise ValueError(f'{path}:{reader.line_num}: 采样行的 Index 或 RawTime 列无效') from exc
        yield index, raw_time, row


def _scan_part(path, encoding, delimiter):
    with path.open(encoding=encoding, newline='') as stream:
        reader = csv.reader(stream, delimiter=delimiter)
        index_column, time_column, channels, metadata = _read_header(reader, path)
        first_index = last_index = first_time = last_time = None
        count = 0
        precise = True
        intervals = []
        for index, raw_time, _ in _sample_rows(reader, path, index_column, time_column):
            if last_index is not None and index <= last_index:
                raise ValueError(f'{path}:{reader.line_num}: Index 必须严格递增')
            if first_index is None:
                first_index, first_time = index, raw_time
            if raw_time is None or (last_time is not None and raw_time <= last_time):
                precise = False
            if precise and last_time is not None:
                intervals.append((raw_time - last_time) / (index - last_index))
            last_index, last_time = index, raw_time
            count += 1
    if not count:
        raise ValueError(f'{path}: 无采样数据')
    # Round the inferred nominal period to microseconds to avoid extending jitter.
    interval_ns = max(1, round(statistics.median(intervals) / 1000) * 1000) if precise and intervals else None
    settings = tuple(metadata.get(key, '').casefold() for key in CAPTURE_SETTINGS)
    return TablePart(path, encoding, delimiter, channels, settings,
                     _decimal(metadata.get('startrawtime', '')), first_index, last_index,
                     count, (first_time, last_time) if precise else None, interval_ns)


def _prepare_parts(tables):
    parts = sorted((_scan_part(path, *fmt) for path, fmt in tables),
                   key=lambda part: (part.first_index, part.path.name))
    ordered, duplicates = [], []
    reference = parts[0]
    starts = [part.capture_start for part in parts if part.capture_start]
    capture_start = min(starts, key=lambda value: value.as_tuple().exponent) if starts else None
    for part in parts:
        if (part.settings != reference.settings
                or {name: kind for name, (_, kind) in part.channels.items()}
                != {name: kind for name, (_, kind) in reference.channels.items()}):
            raise ValueError(f'{part.path}: 采样配置或变量列与 {reference.path.name} 不一致，请分开采集运行')
        if part.capture_start and capture_start:
            unit = Decimal(10) ** part.capture_start.as_tuple().exponent
            if abs(part.capture_start - capture_start) > unit / 2:
                raise ValueError(f'{part.path}: StartRawTime 指向另一轮采集，请分目录运行')
        if ordered and part.first_index <= ordered[-1].last_index:
            previous = ordered[-1]
            if (part.first_index, part.last_index) == (previous.first_index, previous.last_index) and (
                    _file_digest(part.path) == _file_digest(previous.path)):
                duplicates.append(part.path.name)
                continue
            raise ValueError(f'{part.path}: Index 与 {previous.path.name} 重叠，请检查重复导出或混入其他采集')
        ordered.append(part)
    return ordered, duplicates


def _sample_period(parts, sample_interval_ms):
    if sample_interval_ms is not None:
        try:
            period_ns = round(Decimal(str(sample_interval_ms)) * NS_PER_MS)
        except (InvalidOperation, ValueError, OverflowError) as exc:
            raise ValueError('采样间隔须是有限正数（毫秒）') from exc
        if period_ns <= 0:
            raise ValueError('采样间隔须是有限正数（毫秒）')
        return period_ns, 'specified'
    intervals = [part.interval_ns for part in parts if part.interval_ns is not None]
    if intervals:
        return round(statistics.median(intervals)), 'raw_time'
    anchors = [part for part in parts if part.raw_range is not None]
    if len(anchors) > 1:
        first, last = anchors[0], anchors[-1]
        return max(1, (last.raw_range[0] - first.raw_range[0]) //
                   (last.first_index - first.first_index)), 'raw_time'
    return None, None


def _estimate_time(index, left, right, interval_ns, first_index):
    if left is not None and right is not None:
        left_idx, left_ns = left
        right_idx, right_ns = right
        return left_ns + (index - left_idx) * (right_ns - left_ns) // (right_idx - left_idx)
    anchor_idx, anchor_ns = left or right or (first_index, 0)
    return anchor_ns + (index - anchor_idx) * interval_ns


class _Channel:
    def __init__(self):
        self.jumps = []
        self.segments = []
        self.dwell_starts = {}
        self.estimated_dwells = set()
        self.unique_values = set()
        self.count = 0
        self.first_ts = self.last_ts = self.start_ts = self.value = None
        self.start_estimated = False

    def _close_segment(self, end_ts):
        self.segments.append((self.value, self.start_ts, end_ts, end_ts - self.start_ts))

    def append(self, index, value, ts, estimated, continuous):
        if self.start_ts is not None and (not continuous or value is None):
            self._close_segment(self.last_ts)
            self.start_ts = None
        if value is None:
            return
        if self.start_ts is not None and value != self.value:
            self._close_segment(ts)
            self.jumps.append((index, self.value, value, ts))
            self.dwell_starts[index] = self.start_ts
            if estimated or self.start_estimated:
                self.estimated_dwells.add(index)
            self.start_ts = None
        if self.start_ts is None:
            self.value, self.start_ts, self.start_estimated = value, ts, estimated
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts
        self.count += 1
        self.unique_values.add(value)

    def result(self):
        if self.start_ts is not None:
            self._close_segment(self.last_ts)
        return {
            'jumps': self.jumps,
            'segments': self.segments,
            'dwell_starts': self.dwell_starts,
            'estimated_dwells': self.estimated_dwells,
            'sample_count': self.count,
            'time_range': (self.first_ts or 0, self.last_ts or 0),
            'inactive': not self.jumps,
            'unique_values': sorted(self.unique_values),
        }


def parse_omron_trace(path, *, sample_interval_ms=None, matches=None):
    """Return variable series and the input audit for one continuous recording."""
    path = Path(path)
    tables, ignored = _find_tables(path)
    parts, duplicates = _prepare_parts(tables)
    channels = {name: _Channel() for name in parts[0].channels if matches is None or matches(name)}
    if not channels:
        raise ValueError(f'{path}: 找不到符合范围的整型步序变量列')
    interval_ns, interval_source = _sample_period(parts, sample_interval_ms)
    if any(part.raw_range is None for part in parts):
        if parts[0].settings[1] not in ('', 'interval', 'cyclic', 'time'):
            raise ValueError(f'{path}: 非固定间隔采集的 RawTime 精度不足，请重新导出原始 CSV')
        if interval_ns is None:
            raise ValueError(f'{path}: RawTime 精度不足。请加入精确时间分卷，或指定 --sample-interval-ms')

    right_anchors = [None] * len(parts)
    next_anchor = None
    for pos in reversed(range(len(parts))):
        part = parts[pos]
        right_anchors[pos] = next_anchor
        if part.raw_range is not None:
            next_anchor = (part.first_index, part.raw_range[0])

    first_index = parts[0].first_index
    origin = parts[0].raw_range[0] if parts[0].raw_range is not None else (
        _estimate_time(first_index, None, right_anchors[0], interval_ns, first_index))
    previous_index = previous_ns = left_anchor = None
    file_audit, gaps = [], []
    for part, right_anchor in zip(parts, right_anchors):
        estimated = part.raw_range is None
        with part.path.open(encoding=part.encoding, newline='') as stream:
            reader = csv.reader(stream, delimiter=part.delimiter)
            index_column, time_column, columns, _ = _read_header(reader, part.path)
            selected = [(name, columns[name][0], channel) for name, channel in channels.items()]
            count = 0
            for index, raw_time, row in _sample_rows(reader, part.path, index_column, time_column):
                if count == 0 and index != part.first_index:
                    raise ValueError(f'{part.path}: 解析期间首个采样索引变化，请在导出完成后重试')
                ns = _estimate_time(index, left_anchor, right_anchor, interval_ns, first_index) if estimated else raw_time
                if ns is None or (previous_ns is not None and ns <= previous_ns):
                    raise ValueError(f'{part.path}:{reader.line_num}: 合并后的时间戳没有递增，请检查采集分卷')
                if previous_index is not None and index <= previous_index:
                    raise ValueError(f'{part.path}:{reader.line_num}: 合并后的 Index 没有递增')
                continuous = previous_index is not None and index == previous_index + 1
                if previous_index is not None and not continuous:
                    gaps.append({'after_index': previous_index, 'before_index': index})
                ts = (ns - origin + NS_PER_MS // 2) // NS_PER_MS
                for name, column, channel in selected:
                    try:
                        cell = row[column].strip()
                        value = int(cell) if cell else None
                    except (IndexError, ValueError) as exc:
                        raise ValueError(f'{part.path}:{reader.line_num}: {name} 的整型值无效') from exc
                    channel.append(index, value, ts, estimated, continuous)
                previous_index, previous_ns = index, ns
                count += 1
            if count != part.row_count or previous_index != part.last_index:
                raise ValueError(f'{part.path}: 解析期间分卷范围变化，请在导出完成后重试')
        if not estimated:
            left_anchor = (part.last_index, part.raw_range[1])
        file_audit.append({'file': part.path.name, 'first_index': part.first_index,
                           'last_index': part.last_index, 'samples': part.row_count,
                           'time_basis': 'estimated_by_index' if estimated else 'raw_time_ns'})

    audit = {
        'format': 'omron_sysmac_table',
        'time_basis': 'relative_ms',
        'sample_interval_ms': interval_ns / NS_PER_MS if interval_ns is not None else None,
        'interval_source': interval_source,
        'samples_total': sum(part.row_count for part in parts),
        'files': file_audit,
        'duplicate_files': duplicates,
        'ignored_tables': ignored,
        'index_gaps': gaps,
    }
    return {name: channel.result() for name, channel in channels.items()}, audit


# ---- Common step-pair extraction ----


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
    exact = si_map.get(var_name)
    if exact and exact.get('display'):
        return exact['display']
    root = _var_root(var_name)
    if var_name.startswith(root + '[') and '.' not in var_name:
        return var_name
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
    '首次时间ms', '末次时间ms', '所属周期数', '备注', '估算时长次数',
]


def generate(trace_path, run, source=None, proc_name=None,
             idle_threshold_ms=5000, *, pattern=None, sample_interval_ms=None):
    """Run the full trace → pairs extraction.

    Parameters
    ----------
    trace_path : str
        TwinCAT Scope .trace file, Sysmac CSV/TSV/TXT table, or a directory
        of Sysmac tables from one recording.
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
    pattern : str, optional
        Workflow glob or re: pattern applied to the full variable name.
    sample_interval_ms : float, optional
        Sysmac sample period for timing reconstruction when RawTime is rounded.

    Returns
    -------
    dict with run stats and output paths.
    """
    from trace_jump_analyzer import parse_trace, detect_cycles, _write_csv

    try:
        from utils import load_station_identity, infer_proc_name, em_sort_key_prefixed
    except ModuleNotFoundError as exc:
        if exc.name != 'utils':
            raise
        # Earlier workflow versions keep these helpers in the workstation writer.
        from gen_workstation_xlsx import load_station_identity, _em_sort_key as em_sort_key_prefixed

        def infer_proc_name(source, run, proc_name):
            return proc_name or (Path(source).stem if source else Path(run).name)

    run = Path(run)
    matches = None
    if pattern:
        from candidate_filter import glob_regex
        matches = glob_regex(pattern).fullmatch

    # ---- 1. Normalize the trace input ----
    if is_omron_trace_input(trace_path):
        variables, trace_input = parse_omron_trace(
            trace_path, sample_interval_ms=sample_interval_ms, matches=matches)
    else:
        variables = parse_trace(trace_path)
        if matches is not None:
            variables = {name: vd for name, vd in variables.items() if matches(name)}
        trace_input = {'format': 'twincat_scope_xml'}
    run.mkdir(parents=True, exist_ok=True)
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
    pair_estimated = defaultdict(int)

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
        dwell_starts = vd.get('dwell_starts')
        estimated_dwells = vd.get('estimated_dwells', ())
        for k in range(len(jumps)):
            idx, from_v, to_v, ts = jumps[k]
            # Skip degenerate self-loops (from==to).  These arise when a
            # variable briefly flickers and returns to the same value within
            # one sample period — they are not real state transitions.
            if from_v == to_v:
                continue
            # Duration = how long the variable stayed at from_v before
            # moving to to_v.  For k=0 there is no previous jump, so use
            # the variable's first-sample timestamp (initial state dwell
            # time from sampling start to first transition).
            if dwell_starts is not None:
                prev_ts = dwell_starts[idx]
            else:
                prev_ts = first_sample_ts if k == 0 else jumps[k - 1][3]
            dur = ts - prev_ts
            key = (name, from_v, to_v)
            pair_durations[key].append(int(dur))
            if idx in estimated_dwells:
                pair_estimated[key] += 1
            if key not in pair_first_ts:
                pair_first_ts[key] = int(prev_ts)
            pair_last_ts[key] = int(ts)
            pair_cycles[key].add(_cycle_of(ts))

    # ---- 6. Load source-derived evidence (action map, step comments, identity) ----
    amap = _load_action_map(str(run))
    sc_map = _load_step_comments(str(run))
    si_map = load_station_identity(str(run))
    var_docs = _load_var_docs(str(run))

    mapped_vars = {sv for sv, _ in amap}

    # ---- 7. Assemble pair rows ----
    rows = []
    review_rows = []
    for key, durs in pair_durations.items():
        var_name, from_v, to_v = key
        from_int = _val_to_int(from_v)
        to_int = _val_to_int(to_v)
        start_label = _label(var_name, from_int)
        end_label = _label(var_name, to_int)
        is_reset = (to_int == 0)

        # Some source action maps store the array name without its index.
        step_value = str(from_int)
        bare_var = re.sub(r'\[\d+\]', '', var_name)
        action_vars = amap.get((var_name, step_value), [])
        used_unindexed_mapping = False
        if not action_vars and bare_var != var_name:
            action_vars = amap.get((bare_var, step_value), [])
            used_unindexed_mapping = bool(action_vars)

        # The measured dwell and its step comment belong to the start state.
        sc = sc_map.get(start_label, '')

        # Station name for grouping
        ws_name = _station_name(var_name, si_map, var_docs)

        median_dur = int(round(statistics.median(durs)))
        min_dur = int(min(durs))
        max_dur = int(max(durs))
        n_cycles = len(pair_cycles[key])

        remark = []
        if is_reset:
            remark.append('复位退出')
        estimated_count = pair_estimated.get(key, 0)
        if estimated_count:
            remark.append(f'含估算时长（{estimated_count}/{len(durs)} 次）')
        if used_unindexed_mapping:
            remark.append('动作变量命中无下标映射（槽位待核）')
        if not action_vars:
            if var_name in mapped_vars or bare_var in mapped_vars:
                remark.append('该步值无动作变量')
                review_type = 'no_action_for_step_value'
                review_note = f'{start_label} 在源码动作变量映射中无对应动作'
            else:
                remark.append('源码无动作变量映射')
                review_type = 'no_action_mapping'
                review_note = f'trace变量 {var_name} 不在源码动作变量映射中'
            if not sc:
                remark.append('无步注释')
            review_rows.append({
                '类型': review_type,
                '变量': var_name,
                '开始标签': start_label,
                '结束标签': end_label,
                '说明': review_note,
            })

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
            '首次时间ms': pair_first_ts[key],
            '末次时间ms': pair_last_ts[key],
            '所属周期数': n_cycles,
            '备注': ' | '.join(remark),
            '估算时长次数': estimated_count,
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
        'trace_input': trace_input,
        'run': str(run),
        'proc_name': proc,
        'variables_total': len(variables),
        'variables_active': active_count,
        'variables_inactive': inactive_count,
        'total_jumps': len(jump_rows),
        'total_segments': len(seg_rows),
        'cycles_detected': len(cycle_data),
        'unique_pairs': len(rows),
        'pairs_with_estimated_duration': len(pair_estimated),
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
            'summary': str(run / 'trace_pairs_summary.json'),
        },
    }
    (run / 'trace_pairs_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    return summary


def analyze_for_workflow(trace_path, run, *, source=None, pattern=None, sample_interval_ms=None):
    """Return the workbook timing mode and artifacts for the v9.25 workflow."""
    if is_omron_trace_input(trace_path):
        stats = generate(trace_path, run, source=source, pattern=pattern,
                         sample_interval_ms=sample_interval_ms)
        return 'sysmac', stats

    from trace_jump_analyzer import analyze

    run = Path(run)
    stats = analyze(str(trace_path), str(run / 'final_prediction_pairs.csv'),
                    str(run / 'trace_analysis'))
    return 'legacy', stats


def load_trace_timing(run):
    """Read median durations and the pairs that include estimated timing."""
    p = Path(run) / 'trace_collection_pairs.csv'
    durations, estimated = {}, set()
    if not p.is_file():
        return durations, estimated
    with p.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            dur = (row.get('中位数时长ms') or '').strip()
            key = (row.get('开始标签', ''), row.get('结束标签', ''))
            try:
                durations[key] = int(dur) if dur else None
            except ValueError:
                durations[key] = None
            estimate_count = (row.get('估算时长次数') or '').strip()
            if estimate_count.isdecimal() and int(estimate_count) > 0:
                estimated.add(key)
    return durations, estimated


def load_trace_duration_map(run):
    """Return {(start_label, end_label): median_duration_ms} for Excel backfill."""
    return load_trace_timing(run)[0]


def main():
    import argparse
    ap = argparse.ArgumentParser(description='trace → step pairs extraction')
    ap.add_argument('--trace', required=True, help='.trace 文件、Sysmac CSV/TSV/TXT，或同次采集的分卷目录')
    ap.add_argument('--run', required=True, help='run目录（含源码证据文件）')
    ap.add_argument('--source', default=None, help='源码XML（推断工序名用）')
    ap.add_argument('--proc-name', default=None, help='工序名')
    ap.add_argument('--idle-threshold', type=int, default=5000,
                    help='周期检测空闲阈值(ms)，默认5000')
    ap.add_argument('--pattern', help='完整变量名的 glob 或 re: 模式')
    ap.add_argument('--sample-interval-ms', type=float,
                    help='RawTime 精度不足时使用的 Sysmac 采样间隔(ms)')
    a = ap.parse_args()
    stats = generate(a.trace, a.run, a.source, a.proc_name,
                     idle_threshold_ms=a.idle_threshold, pattern=a.pattern,
                     sample_interval_ms=a.sample_interval_ms)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
