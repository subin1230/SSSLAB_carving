"""Single-block payload checks; evidence remains conditional on local boundaries."""
import struct
import zlib
from validator import validate_block as validate_v3
from validator_v1 import result

VERSION = 'v4'
MAX_INFLATED = 1048576


def directory_links(block):
    """Return local offsets referenced by a complete classic single-disk directory.

    Infer archive base from EOCD offsets; never assume block offset == file offset.
    ZIP64, spanning, incomplete directories and optional intervening records abstain.
    """
    links = {}
    for e in range(max(0, len(block)-21)):
        if block[e:e+4] != b'PK\x05\x06':
            continue
        disk, sd, here, total, size, start, comment = struct.unpack_from('<4H2IH', block, e+4)
        if disk or sd or here != total or total in (0, 65535) or e+22+comment > len(block):
            continue
        if size == 0xffffffff or start == 0xffffffff or size > e:
            continue
        cursor = e-size
        base = cursor-start
        records = []
        for _ in range(total):
            if cursor+46 > e or block[cursor:cursor+4] != b'PK\x01\x02':
                break
            nl, xl, cl, disk_start = struct.unpack_from('<4H', block, cursor+28)
            end = cursor+46+nl+xl+cl
            rel = int.from_bytes(block[cursor+42:cursor+46], 'little')
            if end > e or disk_start or rel == 0xffffffff:
                break
            records.append((base+rel, cursor))
            cursor = end
        else:
            if cursor == e:
                for local, central in records:
                    if 0 <= local and local+30 <= e-size and block[local:local+4] == b'PK\x03\x04':
                        nl, xl = struct.unpack_from('<HH', block, local+26)
                        cn = int.from_bytes(block[central+28:central+30], 'little')
                        if local+30+nl+xl <= e-size and block[local+30:local+30+nl] == block[central+46:central+46+cn]:
                            links[local] = central
    return links


def validate_block(block, fmt, offset=None):
    block = bytes(block)
    rows = validate_v3(block, fmt, offset)
    fmt = fmt.upper()
    anchors = {r['offset']: r for r in rows if r['offset'] is not None}
    links = directory_links(block) if fmt == 'ZIP' else {}

    def add(pos, rule, status, reason, support=None):
        ref = anchors[pos]
        evidence = support or ref['boundary_evidence']
        supported = evidence not in ('signature_only', 'inside_crc_checked_payload', 'no_candidate')
        row = result(fmt, pos, rule, status, reason, ref['context'])
        row.update(boundary_evidence=evidence, evidence_status=status if supported else 'UNKNOWN',
                   boundary_note='단일 블록 내부의 조건부 구조 증거. 원본 포맷 확정·FFC 후보 자동 기각 아님')
        rows.append(row)

    for pos in sorted(anchors):
        if fmt == 'PNG':
            if pos+8 > len(block):
                continue
            n = int.from_bytes(block[pos:pos+4], 'big')
            kind = block[pos+4:pos+8]
            checks = {b'sRGB': (1, 'SRGB_INTENT', lambda p: p[0] <= 3),
                      b'pHYs': (9, 'PHYS_UNIT', lambda p: p[8] in (0, 1)),
                      b'gAMA': (4, 'GAMA_VALUE', lambda p: int.from_bytes(p, 'big') > 0),
                      b'tIME': (7, 'TIME_FIELDS', lambda p: 1 <= p[2] <= 12 and 1 <= p[3] <= 31 and p[4] <= 23 and p[5] <= 59 and p[6] <= 60)}
            if kind in checks:
                size, rule, check = checks[kind]
                if n == size:
                    payload = block[pos+8:pos+8+size]
                    status = 'UNKNOWN' if len(payload) < size else 'VALID' if check(payload) else 'INVALID'
                    add(pos, rule, status, f'{kind.decode()} 내부 필드 허용값 검사; 필요={size}, 확보={len(payload)}')
            continue
        if block[pos:pos+4] != b'PK\x03\x04' or pos+30 > len(block):
            continue
        flags, method = struct.unpack_from('<HH', block, pos+6)
        crc, cs, us = struct.unpack_from('<III', block, pos+14)
        nl, xl = struct.unpack_from('<HH', block, pos+26)
        start = pos+30+nl+xl
        support = 'zip_directory_reference' if pos in links else None
        if support:
            c = links[pos]
            same = block[pos+6:pos+10] == block[c+8:c+12] and block[pos+14:pos+26] == block[c+16:c+28]
            # Descriptor mode permits local CRC/size placeholders.
            if not flags & (8 | 8192):
                add(pos, 'LOCAL_CENTRAL_FIELDS', 'VALID' if same else 'INVALID', 'EOCD·중앙 디렉터리 위치 참조와 파일명으로 연결한 로컬/중앙 flags·method·CRC·size 비교', support)
        if flags & (1 | 8 | 64 | 8192) or 0xffffffff in (cs, us) or method not in (0, 8):
            add(pos, 'ENTRY_PAYLOAD_CHECK', 'UNKNOWN', '암호화·descriptor·ZIP64·미지원 압축은 검사 보류', support)
            continue
        if start+cs > len(block):
            add(pos, 'ENTRY_PAYLOAD_CHECK', 'UNKNOWN', '선언된 헤더·항목 데이터가 블록 밖으로 이어짐', support)
            continue
        payload = block[start:start+cs]
        if method == 8:
            try:
                decoder = zlib.decompressobj(-15)
                payload = decoder.decompress(payload, MAX_INFLATED+1)
                if len(payload) > MAX_INFLATED:
                    add(pos, 'ENTRY_PAYLOAD_CHECK', 'UNKNOWN', '압축 해제 출력 예산 초과', support)
                    continue
                ok = decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
            except zlib.error:
                ok = False
            add(pos, 'DEFLATE_STREAM', 'VALID' if ok else 'INVALID', '완전히 확보한 압축 구간의 raw DEFLATE 종료·범위 검사', support)
            if not ok:
                continue
        add(pos, 'ENTRY_UNCOMPRESSED_SIZE', 'VALID' if len(payload) == us else 'INVALID', f'실제 해제 크기={len(payload)}, 선언={us}', support)
        actual = zlib.crc32(payload) & 0xffffffff
        add(pos, 'ENTRY_CRC32', 'VALID' if actual == crc else 'INVALID', f'항목 데이터 CRC 계산={actual:08x}, 선언={crc:08x}', support)
    return rows
