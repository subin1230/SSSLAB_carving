import contextlib
import csv
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

import numpy as np

from zip_pair_candidates import build, enumerate_queries, scan_headers
from zip_pair_payload import check, compare, payload_check


def archive_blocks(method=zipfile.ZIP_STORED, seed=0, size=700, prefix=0):
    stream = io.BytesIO()
    payload = np.random.default_rng(seed).integers(0, 256, size, dtype=np.uint8).tobytes()
    with zipfile.ZipFile(stream, 'w') as z:
        for name, data in [('entry.bin', payload), ('next.txt', b'next payload')]:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = method
            z.writestr(info, data)
    data = bytes(prefix)+stream.getvalue()
    return np.frombuffer(data+bytes((-len(data)) % 512), dtype=np.uint8).reshape(-1, 512).copy()


def first_query(x):
    records, _ = scan_headers(x)
    return next(q for q in enumerate_queries(records)
                if q['rule'] == 'zip_next' and q['source_sample_id'] == 0)


def run_check_for(x, q, candidate=None, limit=8*1024*1024):
    t = candidate if candidate is not None else q['candidates'][0]
    return payload_check(x[q['source_sample_id']].tobytes(), x[t['target_sample_id']].tobytes(), q, t, limit)


class ZipPairPayloadTests(unittest.TestCase):
    def test_real_stored_and_deflate_pairs_and_wrong_target_bytes(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(method=method):
                x = archive_blocks(method)
                q = first_query(x)
                self.assertEqual(q['target_block_delta'], 1)
                good = run_check_for(x, q)
                self.assertEqual(good['status'], 'PASS')
                self.assertGreater(good['payload_bytes_from_target'], 0)
                x[1, 10] ^= 1  # payload changed, the next header stays intact
                bad = run_check_for(x, q)
                self.assertEqual(bad['status'], 'FAIL')
                self.assertIn(bad['reason'], ('PAYLOAD_CRC_MISMATCH', 'DEFLATE_STREAM_ERROR'))

    def test_same_target_position_does_not_imply_matching_payload(self):
        a = archive_blocks(seed=1)
        b = archive_blocks(seed=2)
        x = np.concatenate([a, b])
        q = first_query(x)
        self.assertEqual(len(q['candidates']), 2)
        results = [run_check_for(x, q, t)['status'] for t in q['candidates']]
        self.assertEqual(results, ['PASS', 'FAIL'])

    def test_duplicate_payload_from_different_archive_still_passes(self):
        a = archive_blocks()
        x = np.concatenate([a, a])
        q = first_query(x)
        self.assertEqual(len(q['candidates']), 2)
        self.assertTrue(all(run_check_for(x, q, t)['status'] == 'PASS' for t in q['candidates']))

    def test_missing_middle_block_is_not_concatenated_or_rejected(self):
        x = archive_blocks(size=1400)
        q = first_query(x)
        self.assertGreater(q['target_block_delta'], 1)
        r = run_check_for(x, q)
        self.assertEqual(r['status'], 'UNKNOWN')
        self.assertEqual(r['reason'], 'MISSING_MIDDLE_BLOCKS_OR_NONADJACENT')

    def test_source_only_crc_is_not_evidence_for_target(self):
        x = archive_blocks(size=473)  # 30+9+473 = 512; data ends exactly at boundary
        q = first_query(x)
        self.assertEqual(q['target_offset'], 0)
        r = run_check_for(x, q)
        self.assertEqual(r['status'], 'UNKNOWN')
        self.assertEqual(r['reason'], 'NO_TARGET_PAYLOAD_BYTES')

    def test_empty_deflate_output_is_not_claimed_as_pair_support(self):
        x = archive_blocks(zipfile.ZIP_DEFLATED, size=0, prefix=480)
        q = first_query(x)
        r = run_check_for(x, q)
        self.assertEqual(r['status'], 'UNKNOWN')
        self.assertEqual(r['reason'], 'EMPTY_OUTPUT_NOT_DISCRIMINATIVE')

    def test_output_limit_descriptor_and_size_mismatch(self):
        x = archive_blocks(zipfile.ZIP_DEFLATED)
        q = first_query(x)
        self.assertEqual(run_check_for(x, q, limit=64)['reason'], 'DECLARED_OUTPUT_EXCEEDS_RESOURCE_LIMIT')
        x[0, 6] |= 8
        self.assertEqual(run_check_for(x, q)['status'], 'UNKNOWN')
        x[0, 6] &= 247
        x[0, 22:26] = np.frombuffer(struct.pack('<I', 699), dtype=np.uint8)
        self.assertEqual(run_check_for(x, q)['reason'], 'OUTPUT_EXCEEDS_DECLARED_SIZE')

    def test_check_without_truth_and_paired_evaluation(self):
        a, b = archive_blocks(seed=1), archive_blocks(seed=2)
        self.assertEqual(len(a), len(b))
        x = np.concatenate([a, b])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocks = root/'blocks.npy'
            np.save(blocks, x)
            with contextlib.redirect_stdout(io.StringIO()):
                build(blocks, root/'candidates')
                checked = check(blocks, root/'candidates', root/'payload')
            self.assertFalse((root/'truth.csv').exists())
            self.assertFalse(checked['truth_used'])
            self.assertEqual(checked['by_rule']['zip_next']['PASS'], 2)
            self.assertEqual(checked['by_rule']['zip_next']['FAIL'], 2)
            with (root/'truth.csv').open('w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['sample_id', 'source_file_id', 'source_byte_offset'])
                writer.writeheader()
                for i in range(len(x)):
                    writer.writerow(dict(sample_id=i, source_file_id='a' if i < len(a) else 'b',
                                         source_byte_offset=i % len(a)*512))
            with contextlib.redirect_stdout(io.StringIO()):
                report = compare(blocks, root/'truth.csv', root/'payload')
            removed = report['removed_by_conditional_failure']['zip_next']
            self.assertEqual(removed['wrong_file_relations'], 2)
            self.assertEqual(removed['exact_target_relations'], 0)
            self.assertEqual(report['variants']['payload_passed']['by_rule']['zip_next']['exact_target_precision'], 1)
            with self.assertRaises(FileExistsError):
                compare(blocks, root/'truth.csv', root/'payload')

    def test_saved_variant_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            np.save(root/'blocks.npy', archive_blocks())
            with contextlib.redirect_stdout(io.StringIO()):
                build(root/'blocks.npy', root/'candidates')
                check(root/'blocks.npy', root/'candidates', root/'payload')
            with (root/'payload/baseline/queries.jsonl').open('a') as f:
                f.write('\n')
            with self.assertRaisesRegex(ValueError, 'variants changed'):
                compare(root/'blocks.npy', root/'nonexistent-truth.csv', root/'payload')


if __name__ == '__main__':
    unittest.main()
