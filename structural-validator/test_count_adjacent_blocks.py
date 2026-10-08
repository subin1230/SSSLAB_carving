import unittest
from count_adjacent_blocks import analyze, runs


class AdjacencyTests(unittest.TestCase):
    def test_runs(self):
        self.assertEqual(runs([0, 1, 2, 5, 7, 8]), [3, 1, 2])
        self.assertEqual(runs([]), [])

    def test_pairs_within_and_across_splits(self):
        rows = [('train', 'f1', 'png', 0), ('train', 'f1', 'png', 1), ('train', 'f1', 'png', 2),
                ('train', 'f2', 'zip', 4), ('test', 'f2', 'zip', 5), ('test', 'f3', 'png', 9)]
        s = analyze(rows)
        train_png = s['within_split']['train']['png']
        self.assertEqual(train_png['adjacent_pairs'], 2)
        self.assertEqual(train_png['files_with_first_block'], 1)
        self.assertEqual(train_png['longest_contiguous_run'], 3)
        self.assertEqual(s['within_split']['train']['zip']['adjacent_pairs'], 0)
        # f2의 4,5는 split이 달라서 합쳤을 때만 연속
        self.assertEqual(s['all_splits_combined']['zip']['adjacent_pairs'], 1)
        self.assertEqual(s['files_in_multiple_splits'], {'zip': 1, 'ALL': 1})

    def test_conflicting_format_rejected(self):
        with self.assertRaises(ValueError):
            analyze([('test', 'f1', 'png', 0), ('test', 'f1', 'zip', 1)])
