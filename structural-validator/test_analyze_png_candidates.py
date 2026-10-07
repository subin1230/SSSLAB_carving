import unittest
try:
    import numpy
except ImportError:
    numpy=None


@unittest.skipIf(numpy is None,'requires numpy')
class DetailTests(unittest.TestCase):
    def test_text_length_is_reported_not_classified_as_violation(self):
        from analyze_png_candidates import candidate_details
        from audit_clues import describe
        from validator_v4 import validate_block
        data=b'<photoshop:ICCProfile>sRGB IEC6196'
        category,cs=describe(data,'png',validate_block(data,'png'))
        self.assertEqual(category,'declared_extent_outside_block')
        d=candidate_details(data,cs[0])
        self.assertEqual(d['length_ascii'],"b'ile>'")
        self.assertTrue(d['length_bytes_all_printable'])
        self.assertTrue(all(r['evidence_status']=='UNKNOWN' for r in d['rules']))

    def test_binary_crossing_length(self):
        from analyze_png_candidates import candidate_details
        from audit_clues import describe
        from validator_v4 import validate_block
        data=(1000).to_bytes(4,'big')+b'IDAT'+b'abc'
        _,cs=describe(data,'png',validate_block(data,'png'))
        d=candidate_details(data,cs[0])
        self.assertEqual(d['length'],1000)
        self.assertEqual(d['available_payload_bytes'],3)
        self.assertFalse(d['length_bytes_all_printable'])
