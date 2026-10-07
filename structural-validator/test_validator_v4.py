import io
import struct
import unittest
import zipfile
from validator_v4 import validate_block
from test_validator_v2 import chunk


def archive(method=zipfile.ZIP_STORED, payload=b'hello'):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=method) as z:
        z.writestr('hello.txt', payload)
    return out.getvalue()


class PayloadTests(unittest.TestCase):
    def test_zip_payload_and_references(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            for prefix in (b'', b'prefix'):
                data = prefix + archive(method)
                rows = validate_block(data, 'ZIP')
                self.assertTrue(any(r['rule']=='ENTRY_CRC32' and r['evidence_status']=='VALID' for r in rows))
                self.assertFalse(any(r['evidence_status']=='INVALID' for r in rows))

    def test_real_payload_corruption_supported_by_directory(self):
        data = bytearray(archive())
        data[39] ^= 1
        rows = validate_block(data, 'ZIP')
        self.assertTrue(any(r['rule']=='ENTRY_CRC32' and r['evidence_status']=='INVALID' for r in rows))

    def test_header_disagreement(self):
        data = bytearray(archive())
        data[14] ^= 1
        self.assertTrue(any(r['rule']=='LOCAL_CENTRAL_FIELDS' and r['evidence_status']=='INVALID' for r in validate_block(data,'ZIP')))

    def test_all_truncations_no_false_supported_violation(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            data = archive(method)
            for end in range(len(data)):
                self.assertFalse(any(r['evidence_status']=='INVALID' for r in validate_block(data[:end],'ZIP')), (method,end))

    def test_unanchored_corruption_is_unknown(self):
        data = bytearray(archive()[:44])
        data[39] ^= 1
        rows=validate_block(data,'ZIP')
        self.assertTrue(any(r['rule']=='ENTRY_CRC32' and r['status']=='INVALID' and r['evidence_status']=='UNKNOWN' for r in rows))

    def test_descriptor_encryption_zip64_abstain(self):
        for flags in (1,8,64,8192):
            data=bytearray(archive())
            struct.pack_into('<H',data,6,flags)
            self.assertFalse(any(r['rule']=='ENTRY_CRC32' for r in validate_block(data,'ZIP')))
        data=bytearray(archive())
        struct.pack_into('<I',data,18,0xffffffff)
        self.assertFalse(any(r['rule']=='ENTRY_CRC32' for r in validate_block(data,'ZIP')))

    def test_inflate_budget(self):
        data=archive(zipfile.ZIP_DEFLATED,b'x'*1100000)
        rows=validate_block(data,'ZIP')
        self.assertTrue(any(r['rule']=='ENTRY_PAYLOAD_CHECK' and r['status']=='UNKNOWN' and '예산' in r['reason'] for r in rows))
        self.assertFalse(any(r['rule']=='ENTRY_CRC32' for r in rows))

    def test_png_semantic_fields_and_text(self):
        for kind,payload,rule in [(b'sRGB',b'\x04','SRGB_INTENT'),(b'pHYs',b'\0'*8+b'\x02','PHYS_UNIT'),(b'gAMA',b'\0'*4,'GAMA_VALUE'),(b'tIME',b'\x07\xe8\x0d\x01\0\0\0','TIME_FIELDS')]:
            rows=validate_block(chunk(kind,payload),'PNG')
            self.assertTrue(any(r['rule']==rule and r['evidence_status']=='INVALID' for r in rows))
        rows=validate_block(b'<photoshop:ICCProfile>sRGB IEC6196','PNG')
        self.assertFalse(any(r['evidence_status']=='INVALID' for r in rows))
