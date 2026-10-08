import unittest
from scan_all_formats import detect
from validator_v5 import validate_block
from test_validator_v2 import chunk


class DetectionTests(unittest.TestCase):
    def test_no_candidate(self):
        results, candidates, supported = detect(b'no structure here', validate_block)
        self.assertEqual(candidates, [])
        self.assertEqual(supported, [])
        self.assertEqual(set(results), {'png', 'zip'})

    def test_both_formats_are_checked_and_kept(self):
        data = chunk(b'pHYs', b'\0'*9) + b'PK\x03\x04'
        results, candidates, supported = detect(data, validate_block)
        self.assertEqual(candidates, ['png', 'zip'])
        self.assertEqual(supported, ['png'])

    def test_raw_valid_is_not_qualified_support(self):
        data = chunk(b'IEND', b'')
        results, candidates, supported = detect(data, validate_block)
        self.assertEqual(candidates, ['png'])
        self.assertEqual(supported, [])
        self.assertTrue(any(r['status'] == 'VALID' for r in results['png']))
