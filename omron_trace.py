"""Read Sysmac Studio continuous-trace tables as indexed step observations."""

from __future__ import annotations

import codecs
import csv
import hashlib
import statistics
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


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
