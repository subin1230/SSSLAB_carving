import io
import struct
import unittest
import zipfile
from validator_v5 import validate_block


def sample():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('folder/', b'')
        archive.writestr('folder/a', b'hello')
    data = out.getvalue()
    return data[:data.index(b'PK\x01\x02')]


class AdjacencyTests(unittest.TestCase):
    def links(self, data):
        return [r for r in validate_block(data, 'ZIP') if r['rule'] == 'LOCAL_ENTRY_ADJACENCY']

    def test_empty_directory_followed_by_entry(self):
        rows = self.links(sample())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['next_offset'], 37)
        self.assertEqual(rows[0]['evidence_status'], 'UNKNOWN')

    def test_wrong_size_does_not_create_link(self):
        data = bytearray(sample())
        struct.pack_into('<I', data, 18, 1)
        self.assertEqual(self.links(data), [])

    def test_descriptor_and_truncated_next_header_abstain(self):
        data = bytearray(sample())
        struct.pack_into('<H', data, 6, 8)
        self.assertEqual(self.links(data), [])
        self.assertEqual(self.links(sample()[:41]), [])

    def test_embedded_header_remains_unconfirmed(self):
        rows = self.links(b'random' + sample())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['boundary_evidence'], 'signature_only')
        self.assertEqual(rows[0]['evidence_status'], 'UNKNOWN')
