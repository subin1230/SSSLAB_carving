import unittest,struct
from audit_zip_pilot import references
class ReferenceTests(unittest.TestCase):
    def test_local_and_truncation(self):
        b=bytearray(30);b[:4]=b'PK\x03\x04';struct.pack_into('<I',b,18,100);struct.pack_into('<HH',b,26,5,3)
        self.assertEqual(list(references(b)),[('zip_next',0,138,'relative')])
        self.assertEqual(list(references(b[:20])),[('local',0,None,'partial_header')])
        b[6]=8
        self.assertEqual(list(references(b))[0][3],'unsupported')
