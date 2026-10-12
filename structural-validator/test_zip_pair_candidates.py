import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

import numpy as np

from zip_pair_candidates import build, enumerate_queries, evaluate, scan_headers


def archive_blocks():
    """A real ZIP with records on multiple 512-byte blocks."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_STORED) as z:
        for i in range(6):
            z.writestr(f'entry-{i}.txt', bytes([65+i]) * (700+i*23))
    data = buf.getvalue()
    return np.frombuffer(data + bytes((-len(data)) % 512), dtype=np.uint8).reshape(-1, 512).copy()


def canonical_queries(x, original_ids):
    records, _ = scan_headers(x)
    return {
        (original_ids[q['source_sample_id']], q['source_offset'], q['rule'],
         q['target_offset'], tuple(sorted(
             (original_ids[t['target_sample_id']], t['target_offset'], t['target_kind'])
             for t in q['candidates'])))
        for q in enumerate_queries(records)
    }


class ZipPairCandidateTests(unittest.TestCase):
    def test_shuffling_does_not_change_candidates(self):
        x = np.concatenate([archive_blocks(), archive_blocks()])
        order = np.random.default_rng(27).permutation(len(x))
        self.assertEqual(canonical_queries(x, np.arange(len(x))),
                         canonical_queries(x[order], order))

    def test_duplicate_archives_remain_ambiguous_and_are_penalized(self):
        archive = archive_blocks()
        n = len(archive)
        x = np.concatenate([archive, archive])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocks = root/'blocks.npy'
            np.save(blocks, x)
            with contextlib.redirect_stdout(io.StringIO()):
                # No truth file exists while generation runs.
                summary = build(blocks, root/'candidates')
            self.assertFalse(summary['truth_used'])
            self.assertGreater(summary['by_rule']['cd_local']['ambiguous_queries'], 0)
            with (root/'truth.csv').open('w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['sample_id', 'source_file_id', 'source_byte_offset'])
                writer.writeheader()
                for i in range(len(x)):
                    writer.writerow(dict(sample_id=i, source_file_id='first' if i < n else 'second',
                                         source_byte_offset=(i % n)*512))
            before = (root/'candidates/queries.jsonl').read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                report = evaluate(blocks, root/'truth.csv', root/'candidates')
            cd = report['by_rule']['cd_local']
            self.assertGreater(cd['exact_target_relations'], 0)
            self.assertGreater(cd['wrong_file_relations'], 0)
            self.assertLess(cd['same_file_precision'], 1)
            # entry-2's local filename crosses a block boundary. Its signature
            # exists but the bytes-only name matcher cannot recover that link.
            self.assertEqual(cd['oracle_target_signature_available_queries'], 10)
            self.assertEqual(cd['exact_target_relations'], 8)
            self.assertEqual(cd['available_target_query_recall'], 0.8)
            self.assertEqual(cd['same_file_precision'], 0.5)
            self.assertEqual(before, (root/'candidates/queries.jsonl').read_bytes())
            self.assertGreater(report['by_rule']['eocd']['exact_target_relations'], 0)
            # Input changes must not be silently compared to saved hypotheses.
            x[0, 0] ^= 1
            np.save(blocks, x)
            with self.assertRaisesRegex(ValueError, 'blocks differ'):
                evaluate(blocks, root/'truth.csv', root/'candidates')

    def test_cd_matching_checks_more_than_header_signature(self):
        x = archive_blocks()
        records, _ = scan_headers(x)
        before = [q for q in enumerate_queries(records) if q['rule'] == 'cd_local']
        chosen = next(q for q in before if q['candidates'])
        target = chosen['candidates'][0]
        # Mutate the local CRC; a still-recognizable header must no longer match.
        x[target['target_sample_id'], target['target_offset']+14] ^= 1
        records, _ = scan_headers(x)
        after = next(q for q in enumerate_queries(records)
                     if q['rule'] == 'cd_local' and q['source_sample_id'] == chosen['source_sample_id']
                     and q['source_offset'] == chosen['source_offset'])
        self.assertNotIn(target, after['candidates'])

    def test_eocd_retains_reverse_position_constraint(self):
        records, _ = scan_headers(archive_blocks())
        queries = [q for q in enumerate_queries(records) if q['rule'] == 'eocd']
        self.assertTrue(queries)
        self.assertLess(queries[0]['target_block_delta'], 0)
        # Size inconsistent with source alignment must not generate a link.
        for r in records:
            if r['kind'] == 'eocd':
                r['cd_size'] += 1
        self.assertFalse(any(q['rule'] == 'eocd' for q in enumerate_queries(records)))

    def test_partial_headers_and_descriptors_are_not_interpreted_as_sizes(self):
        x = archive_blocks()
        x[0, 6] |= 8  # local header sizes cannot be used under bit 3
        x[-1, 500:504] = np.frombuffer(b'PK\x03\x04', dtype=np.uint8)
        records, counts = scan_headers(x)
        self.assertFalse(any(r['sample_id'] == 0 and r['offset'] == 0 for r in records))
        self.assertFalse(any(r['offset'] == 500 and r['kind'] == 'local' for r in records))
        self.assertGreater(counts['local | unsupported_or_inconsistent'], 0)
        self.assertGreater(counts['local | partial_fixed_header'], 0)


if __name__ == '__main__':
    unittest.main()
