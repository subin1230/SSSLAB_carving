import random
import struct
import unittest
import zlib
from demo import examples
from validator import validate_block, png_chunk, zip_header


class ValidatorTests(unittest.TestCase):
    def test_demo_cases(self):
        expected = [("CHUNK_CRC", "VALID"), ("CHUNK_CRC", "INVALID"),
                    ("CHUNK_CRC", "UNKNOWN"), ("LOCAL_HEADER_EXTENT", "VALID"),
                    ("STORED_SIZE", "INVALID"), ("LOCAL_HEADER", "UNKNOWN")]
        for (_, fmt, data), pair in zip(examples(), expected):
            with self.subTest(pair=pair):
                self.assertIn(pair, [(r['rule'], r['status']) for r in validate_block(data, fmt)])

    def test_empty_or_middle_block_is_unknown(self):
        for fmt in ("PNG", "ZIP"):
            for data in (b"", b"\x00" * 512):
                self.assertEqual(validate_block(data, fmt)[0]['status'], 'UNKNOWN')

    def test_all_truncations_are_not_invalid(self):
        for index, check in ((0, png_chunk), (3, zip_header)):
            data = examples()[index][2]
            for end in range(len(data) + 1):
                self.assertFalse(any(r['status'] == 'INVALID' for r in check(data[:end])))

    def test_offset_and_candidate_context(self):
        rows = validate_block(b"padding" + examples()[0][2], "PNG")
        self.assertTrue(all(r['offset'] == 7 and r['context'] == 'scanned_candidate' for r in rows))

    def test_ihdr_error_with_recomputed_crc(self):
        data = bytearray(examples()[0][2])
        data[8:12] = b"\0" * 4
        data[-4:] = struct.pack(">I", zlib.crc32(data[4:-4]))
        rows = png_chunk(data)
        self.assertIn(('IHDR_FIELDS', 'INVALID'), [(r['rule'], r['status']) for r in rows])
        self.assertIn(('CHUNK_CRC', 'VALID'), [(r['rule'], r['status']) for r in rows])

    def test_descriptor_does_not_use_local_sizes(self):
        data = bytearray(examples()[4][2])
        data[6:8] = struct.pack('<H', 8)
        rows = zip_header(data)
        self.assertFalse(any(r['status'] == 'INVALID' for r in rows))
        self.assertIn('Data Descriptor', rows[-1]['reason'])

    def test_unsupported_method_is_unknown(self):
        data = bytearray(examples()[3][2])
        data[8:10] = struct.pack('<H', 12345)
        self.assertEqual(zip_header(data)[-1]['status'], 'UNKNOWN')

    def test_extra_field_overflow(self):
        data = bytearray(examples()[3][2][:39])
        data[28:30] = struct.pack('<H', 4)
        data += struct.pack('<HH', 1, 100)
        self.assertEqual(zip_header(data)[-1]['status'], 'INVALID')

    def test_random_data_never_crashes(self):
        rng = random.Random(12)
        for size in range(1024):
            data = bytes(rng.randrange(256) for _ in range(size))
            for fmt in ('PNG', 'ZIP'):
                self.assertTrue(validate_block(data, fmt))


if __name__ == '__main__':
    unittest.main()
