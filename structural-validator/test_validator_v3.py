import struct
import unittest
from validator import validate_block
from test_validator_v2 import chunk


class BoundaryTests(unittest.TestCase):
    def test_xml_srgb_is_conditional_not_supported(self):
        data=b'x'*470+b'<photoshop:ICCProfile>sRGB IEC6196'
        rows=validate_block(data,'PNG')
        bad=[r for r in rows if r['status']=='INVALID']
        self.assertTrue(bad)
        self.assertTrue(all(r['evidence_status']=='UNKNOWN' and r['boundary_evidence']=='signature_only' for r in bad))

    def test_crc_payload_does_not_become_another_chunk(self):
        rows=validate_block(chunk(b'tEXt',b'keyword\0<ICCProfile>sRGB IEC'), 'PNG')
        bad=[r for r in rows if r['status']=='INVALID']
        self.assertTrue(bad)
        self.assertTrue(all(r['boundary_evidence']=='inside_crc_checked_payload' for r in bad))

    def test_anchored_violation_is_preserved(self):
        ihdr=chunk(b'IHDR',struct.pack('>IIBBBBB',16,16,8,2,0,0,0))
        data=b'\x89PNG\r\n\x1a\n'+ihdr+struct.pack('>I',2)+b'sRGB'+b'\0\0'
        bad=[r for r in validate_block(data,'PNG') if r['rule']=='ANCILLARY_LENGTH']
        self.assertEqual(bad[0]['evidence_status'],'INVALID')
        self.assertEqual(bad[0]['boundary_evidence'],'png_signature_chain')

    def test_iend_alone_is_weak(self):
        rows=validate_block(chunk(b'IEND',b''),'PNG')
        self.assertTrue(any(r['status']=='VALID' for r in rows))
        self.assertTrue(all(r['evidence_status']=='UNKNOWN' for r in rows))

    def test_explicit_offset_is_assumption(self):
        rows=validate_block(struct.pack('>I',2)+b'sRGB','PNG',0)
        self.assertTrue(all(r['boundary_evidence']=='caller_assumed' for r in rows))
        self.assertTrue(any(r['evidence_status']=='INVALID' for r in rows))

    def test_no_candidate_and_zip_remain_conservative(self):
        for data,fmt in [(b'none','PNG'),(b'PK\x03\x04'+b'\0'*30,'ZIP')]:
            self.assertTrue(all(r['evidence_status']=='UNKNOWN' for r in validate_block(data,fmt)))
