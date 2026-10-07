import io
import struct
import unittest
import zipfile
import zlib
from validator import validate_block, png_chunk, zip_record, zip_header
from validator_v1 import validate_block as baseline


def chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))


def pairs(rows):
    return {(r['rule'], r['status']) for r in rows}


class V2Tests(unittest.TestCase):
    def test_partial_ihdr_zero_width_without_crc(self):
        data = struct.pack('>I', 13) + b'IHDR' + b'\0' * 4
        self.assertIn(('IHDR_WIDTH', 'INVALID'), pairs(png_chunk(data)))
        self.assertIn(('CHUNK_CRC', 'UNKNOWN'), pairs(png_chunk(data)))
        self.assertFalse(any(r['status']=='INVALID' for r in baseline(data, 'PNG')))

    def test_new_chunks_and_lengths(self):
        data=chunk(b'pHYs', struct.pack('>IIB', 100, 100, 1))
        self.assertIn(('CHUNK_CRC','VALID'), pairs(validate_block(data,'PNG')))
        self.assertEqual(baseline(data,'PNG')[0]['status'],'UNKNOWN')
        self.assertIn(('PLTE_LENGTH','INVALID'),pairs(png_chunk(chunk(b'PLTE',b'1234'))))
        self.assertIn(('ANCILLARY_LENGTH','INVALID'),pairs(png_chunk(chunk(b'gAMA',b'123'))))

    def test_all_new_png_prefixes(self):
        for data in (chunk(b'IHDR',struct.pack('>IIBBBBB',32,32,8,2,0,0,0)), chunk(b'PLTE',b'123'),chunk(b'pHYs',struct.pack('>IIB',100,100,1))):
            for end in range(len(data)+1):
                self.assertFalse(any(r['status']=='INVALID' for r in png_chunk(data[:end])),(end,data))

    def test_partial_local_size_before_filename(self):
        data=bytearray(26)
        data[:4]=b'PK\x03\x04'
        struct.pack_into('<II',data,18,2,3)
        self.assertIn(('LOCAL_PARTIAL_STORED_SIZE','INVALID'),pairs(zip_header(data)))
        for flag in (8,1,64,8192):
            struct.pack_into('<H',data,6,flag)
            self.assertNotIn(('LOCAL_PARTIAL_STORED_SIZE','INVALID'),pairs(zip_header(data)))
        struct.pack_into('<H',data,6,0)
        struct.pack_into('<I',data,18,0xffffffff)
        self.assertNotIn(('LOCAL_PARTIAL_STORED_SIZE','INVALID'),pairs(zip_header(data)))

    def test_real_zip_central_eocd_all_prefixes(self):
        for method in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w',compression=method) as z:
                z.writestr('a.txt',b'hello'*20)
                z.comment=b'comment'
            data=stream.getvalue()
            for sig in (b'PK\x01\x02',b'PK\x05\x06'):
                record=data[data.index(sig):]
                for end in range(len(record)+1):
                    self.assertFalse(any(r['status']=='INVALID' for r in zip_record(record[:end])))
            rows=validate_block(data,'ZIP')
            self.assertIn(('CENTRAL_HEADER_EXTENT','VALID'),pairs(rows))
            self.assertIn(('EOCD_COUNTS','VALID'),pairs(rows))

    def test_eocd_mutation_and_zip64(self):
        data=bytearray(b'PK\x05\x06'+struct.pack('<4H2IH',0,0,1,2,0,0,0))
        self.assertIn(('EOCD_COUNTS','INVALID'),pairs(zip_record(data)))
        struct.pack_into('<H',data,10,0xffff)
        self.assertIn(('EOCD_COUNTS','UNKNOWN'),pairs(zip_record(data)))

    def test_central_extra_overrun(self):
        data=bytearray(46)
        data[:4]=b'PK\x01\x02'
        struct.pack_into('<H',data,30,4)
        data+=struct.pack('<HH',1,20)
        self.assertIn(('CENTRAL_EXTRA_LAYOUT','INVALID'),pairs(zip_record(data)))
