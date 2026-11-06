import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from omron_trace import parse_omron_trace, trace_input_digest


class OmronTraceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.variable = 'Program0.Step[3]'
        self.epoch_ns = 1_700_000_000_000_000_000

    def table(self, name, samples, *, delimiter=',', rounded=False):
        path = self.root / name
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream, delimiter=delimiter)
            writer.writerows([['TraceType', 'Continuous'], ['SampleTimingType', 'interval'],
                              ['SamplingInterval', 3], ['StartRawTime', '1.7E+18']])
            writer.writerow(['Index', 'Date', 'ClockTime', 'RawTime', 'TraceSampID',
                             f'{self.variable}:INT', 'Counter:UINT', 'Temperature:REAL'])
            for index, step in samples:
                raw_time = '1.70E+18' if rounded else self.epoch_ns + index * 12_000_000
                writer.writerow([index, '', '', raw_time, '', step, 42, 21.5])
        return path

    def test_orders_parts_and_keeps_transition_at_boundary(self):
        precise = self.table('z.csv', [(0, 0), (1, 10)])
        self.table('c.csv', [(2, 20), (3, 20)], delimiter='\t', rounded=True)
        self.table('b.tsv', [(4, 30)], delimiter='\t', rounded=True)
        self.table('a.txt', [(5, 0)], delimiter='\t', rounded=True)
        shutil.copyfile(precise, self.root / 'zz-copy.csv')
        (self.root / 'unrelated.csv').write_text('first,second\n', encoding='utf-8')

        variables, audit = parse_omron_trace(self.root)
        channel = variables[self.variable]
        self.assertEqual(channel['jumps'], [(1, 0, 10, 12), (2, 10, 20, 24),
                                           (4, 20, 30, 48), (5, 30, 0, 60)])
        self.assertEqual(channel['estimated_dwells'], {2, 4, 5})
        self.assertEqual(channel['dwell_starts'][4], 24)
        self.assertTrue(variables['Counter']['inactive'])
        self.assertNotIn('Temperature', variables)
        self.assertEqual(audit['sample_interval_ms'], 12)
        self.assertEqual(audit['samples_total'], 6)
        self.assertEqual([item['file'] for item in audit['files']], ['z.csv', 'c.csv', 'b.tsv', 'a.txt'])
        self.assertEqual(audit['duplicate_files'], ['zz-copy.csv'])
        self.assertEqual(audit['ignored_tables'], ['unrelated.csv'])

    def test_missing_indices_and_empty_cells_end_the_observed_segment(self):
        self.table('first.csv', [(0, 10), (1, 20)])
        self.table('later.csv', [(4, 30), (5, 40), (6, None), (7, 50), (8, 60)])
        variables, audit = parse_omron_trace(self.root, matches=lambda name: name == self.variable)
        self.assertEqual(list(variables), [self.variable])
        channel = variables[self.variable]
        self.assertEqual(channel['jumps'], [(1, 10, 20, 12), (5, 30, 40, 60), (8, 50, 60, 96)])
        self.assertEqual(channel['dwell_starts'], {1: 0, 5: 48, 8: 84})
        self.assertEqual(audit['index_gaps'], [{'after_index': 1, 'before_index': 4}])

    def test_all_rounded_timestamps_require_an_interval(self):
        table = self.table('rounded.tsv', [(100, 10), (101, 20)], delimiter='\t', rounded=True)
        with self.assertRaisesRegex(ValueError, 'RawTime 精度不足'):
            parse_omron_trace(table)
        variables, audit = parse_omron_trace(table, sample_interval_ms=12)
        self.assertEqual(variables[self.variable]['jumps'], [(101, 10, 20, 12)])
        self.assertEqual(audit['interval_source'], 'specified')

    def test_rounding_between_two_precise_parts_uses_measured_anchors(self):
        self.table('first.csv', [(0, 0)])
        self.table('rounded.csv', [(1, 10)], rounded=True)
        self.table('last.csv', [(2, 20)])
        variables, audit = parse_omron_trace(self.root)
        self.assertEqual(variables[self.variable]['jumps'], [(1, 0, 10, 12), (2, 10, 20, 24)])
        self.assertEqual(variables[self.variable]['estimated_dwells'], {1, 2})
        self.assertEqual(audit['sample_interval_ms'], 12)

    def test_rejects_conflicting_overlap_and_binds_the_directory_to_files(self):
        self.table('first.csv', [(0, 10), (1, 20)])
        digest = trace_input_digest(self.root)
        self.table('overlap.csv', [(0, 50), (1, 60)])
        self.assertNotEqual(digest, trace_input_digest(self.root))
        with self.assertRaisesRegex(ValueError, 'Index .*重叠'):
            parse_omron_trace(self.root)


if __name__ == '__main__':
    unittest.main()
