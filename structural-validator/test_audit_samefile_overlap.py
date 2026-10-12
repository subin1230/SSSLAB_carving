import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest

import numpy as np

from audit_samefile_overlap import audit


def prepare(root, extra_model_rows=()):
    pilot, cache, data = root/'pilot', root/'cache', root/'data'
    for p in (pilot, cache, data):
        p.mkdir()
    ids, names, kinds = ['uuid-a', 'uuid-b', 'uuid-c', 'uuid-d'], ['jar', 'apk', 'docx', 'xlsx'], [32, 5, 21, 83]
    np.save(cache/'file_code.npy', np.repeat(np.arange(4, dtype=np.int32), 8))
    np.save(cache/'uuid_table.npy', np.array(ids, dtype='S36'))
    np.save(data/'index.npy', np.tile(np.arange(8), 4))
    np.save(data/'filetype_id.npy', np.repeat(kinds, 8))
    # Pilot order is shuffled and does not match source order.
    gids = [9, 1, 26, 18]
    np.save(pilot/'blocks.npy', np.array([[g]*512 for g in gids], dtype=np.uint8))
    with (pilot/'truth.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['sample_id', 'source_file_id', 'source_byte_offset', 'format', 'global_block_id'])
        w.writeheader()
        for i, g in enumerate(gids):
            w.writerow(dict(sample_id=i, source_file_id=ids[g//8], source_byte_offset=g%8*512,
                            format=names[g//8], global_block_id=g))
    with (root/'model_blocks.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['source_id', 'file_type', 'old_filetype_id', 'split',
                                        'source_start_global', 'source_end_global', 'source_local_index', 'global_block_index'])
        w.writeheader()
        for g, split in [(2, 'train'), (9, 'val'), (17, 'test'), *extra_model_rows]:
            w.writerow(dict(source_id=f'source-{g//8}-{split}', file_type=names[g//8], old_filetype_id=kinds[g//8],
                            split=split, source_start_global=g//8*8, source_end_global=g//8*8+8,
                            source_local_index=g%8, global_block_index=g))


def run(root):
    with contextlib.redirect_stdout(io.StringIO()):
        return audit(root/'pilot', root/'model_blocks.csv', root/'cache', root/'data', root/'out')


class SameFileOverlapTests(unittest.TestCase):
    def test_same_file_overlap_found_without_exact_block_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root)
            r = run(root)
            self.assertEqual(r['overlap_files'], dict(train=1, val=1, test=1))
            self.assertEqual(r['exact_block_overlap'], dict(train=0, val=1, test=0))
            self.assertEqual(r['not_observed_in_model_metadata'], 1)
            self.assertEqual(r['by_format']['xlsx']['not_observed_in_model_metadata'], 1)
            self.assertFalse(r['affects_scoring'])
            self.assertTrue(all(v == 0 for v in r['identity_diagnostics'].values()))
            with self.assertRaises(FileExistsError):
                run(root)

    def test_canonical_uuid_overlap_across_model_splits_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root, [(3, 'val')])
            r = run(root)
            self.assertEqual(r['identity_diagnostics']['observed_uuids_in_multiple_model_splits'], 1)
            self.assertEqual(r['overlap_files']['val'], 2)

    def test_stale_cache_identity_fails_before_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root)
            np.save(root/'cache/uuid_table.npy', np.array(['wrong', 'uuid-b', 'uuid-c', 'uuid-d'], dtype='S36'))
            with self.assertRaisesRegex(ValueError, 'pilot truth does not match'):
                run(root)
            self.assertFalse((root/'out').exists())

    def test_model_index_mismatch_is_not_treated_as_no_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare(root)
            values = np.load(root/'data/index.npy')
            values[2] = 7
            np.save(root/'data/index.npy', values)
            with self.assertRaisesRegex(ValueError, 'index/interval mismatch'):
                run(root)


if __name__ == '__main__':
    unittest.main()
