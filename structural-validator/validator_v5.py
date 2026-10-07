"""Record conditional ZIP local-entry adjacency without promoting boundaries."""
import struct
from validator_v4 import validate_block as validate_v4
from validator_v1 import result

VERSION = 'v5'


def local_extent(block, pos):
    if pos + 30 > len(block) or block[pos:pos+4] != b'PK\x03\x04':
        return None
    flags, method = struct.unpack_from('<HH', block, pos+6)
    compressed, uncompressed = struct.unpack_from('<II', block, pos+18)
    name, extra = struct.unpack_from('<HH', block, pos+26)
    if flags & (1 | 8 | 64 | 8192) or method not in (0, 8):
        return None
    if 0xffffffff in (compressed, uncompressed):
        return None
    start = pos + 30 + name + extra
    if start > len(block):
        return None
    return start + compressed


def validate_block(block, fmt, offset=None):
    block = bytes(block)
    rows = validate_v4(block, fmt, offset)
    if fmt.upper() != 'ZIP':
        return rows
    anchors = {r['offset']: r for r in rows
               if r['offset'] is not None and block[r['offset']:r['offset']+4] == b'PK\x03\x04'}
    for pos, ref in sorted(anchors.items()):
        end = local_extent(block, pos)
        # Absence at the declared end is not a contradiction: other ZIP records,
        # padding or a block boundary may follow. Record positive links only.
        if end is None or end not in anchors or local_extent(block, end) is None:
            continue
        row = result('ZIP', pos, 'LOCAL_ENTRY_ADJACENCY', 'VALID',
                     f'선언된 헤더·압축 크기로 계산한 항목 끝={end}; 다음 로컬 헤더 위치와 일치', ref['context'])
        row.update(next_offset=end, boundary_evidence=ref['boundary_evidence'],
                   evidence_status='UNKNOWN',
                   boundary_note='두 후보 사이의 조건부 위치 관계만 확인. 독립 경계 확립·파일 형식 확정·기각 근거 아님')
        rows.append(row)
    return rows
