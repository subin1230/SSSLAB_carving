"""v2: 부분 필드와 추가 레코드의 구조 증거. 블록 전체 판정은 아님."""
import argparse
import json
import struct
from validator_v1 import result, png_chunk as png_v1, zip_header as zip_v1

VERSION = 'v2'
PNG_TYPES = set(b'IHDR PLTE IDAT IEND cHRM gAMA iCCP sBIT sRGB tEXt zTXt iTXt bKGD hIST pHYs sPLT tIME eXIf acTL fcTL fdAT'.split())
FIXED = {b'cHRM': 32, b'gAMA': 4, b'sRGB': 1, b'pHYs': 9, b'tIME': 7, b'acTL': 8, b'fcTL': 26}


def png_chunk(block, offset=0, context='assumed_boundary'):
    rows = png_v1(block, offset, context)
    data = block[offset:]
    if len(data) < 8 or any(r['rule'] in ('CHUNK_LENGTH', 'CHUNK_TYPE') for r in rows):
        return rows
    length, kind = int.from_bytes(data[:4], 'big'), bytes(data[4:8])
    payload = data[8:8 + length]
    def add(rule, ok, reason):
        rows.append(result('PNG', offset, rule, 'UNKNOWN' if ok is None else 'VALID' if ok else 'INVALID', reason, context))
    if kind in FIXED:
        add('ANCILLARY_LENGTH', length == FIXED[kind], f'{kind.decode()} 길이={length}, 요구={FIXED[kind]}')
    if kind == b'PLTE':
        add('PLTE_LENGTH', 3 <= length <= 768 and length % 3 == 0, f'팔레트는 1~256 RGB 항목: 길이={length}')
    if kind == b'IHDR' and length == 13:
        # CRC 또는 뒤 필드가 없더라도 확보한 필드만 독립 검사한다.
        checks = [
            ('IHDR_WIDTH', 4, lambda p: 0 < int.from_bytes(p[:4], 'big') <= 0x7fffffff),
            ('IHDR_HEIGHT', 8, lambda p: 0 < int.from_bytes(p[4:8], 'big') <= 0x7fffffff),
            ('IHDR_DEPTH_COLOR', 10, lambda p: p[8] in {0:{1,2,4,8,16},2:{8,16},3:{1,2,4,8},4:{8,16},6:{8,16}}.get(p[9], set())),
            ('IHDR_COMPRESSION', 11, lambda p: p[10] == 0),
            ('IHDR_FILTER', 12, lambda p: p[11] == 0),
            ('IHDR_INTERLACE', 13, lambda p: p[12] in (0, 1))]
        for rule, needed, check in checks:
            add(rule, check(payload) if len(payload) >= needed else None,
                f'IHDR 데이터 {needed}바이트 필요, 확보={len(payload)}; 해당 필드 명세 검사')
    return rows


def size_rule(data, offset, context, flags_at, method_at, size_at, rule):
    if len(data) < size_at + 8:
        return result('ZIP', offset, rule, 'UNKNOWN', '크기 필드까지의 바이트 부족', context)
    flags = int.from_bytes(data[flags_at:flags_at+2], 'little')
    method = int.from_bytes(data[method_at:method_at+2], 'little')
    csize, usize = struct.unpack_from('<II', data, size_at)
    if flags & (8 | 1 | 64 | 8192) or 0xffffffff in (csize, usize) or method != 0:
        return result('ZIP', offset, rule, 'UNKNOWN', 'Data Descriptor/암호화/ZIP64/압축 항목은 이 크기 비교에서 제외', context)
    return result('ZIP', offset, rule, 'VALID' if csize == usize else 'INVALID',
                  f'비압축·비암호화 크기 비교: {csize} / {usize}', context)


def zip_header(block, offset=0, context='assumed_boundary'):
    rows = zip_v1(block, offset, context)
    data = block[offset:]
    if data[:4] == b'PK\x03\x04' and not any(r['rule'] == 'STORED_SIZE' or r['status'] == 'INVALID' for r in rows):
        rows.append(size_rule(data, offset, context, 6, 8, 18, 'LOCAL_PARTIAL_STORED_SIZE'))
    return rows


def zip_record(block, offset=0, context='assumed_boundary'):
    if not 0 <= offset <= len(block):
        raise ValueError('offset 범위 오류')
    data = block[offset:]
    sig = data[:4]
    if sig not in (b'PK\x01\x02', b'PK\x05\x06'):
        return zip_header(block, offset, context)
    rows = []
    def add(rule, ok, reason):
        rows.append(result('ZIP', offset, rule, 'UNKNOWN' if ok is None else 'VALID' if ok else 'INVALID', reason, context))
    if sig == b'PK\x01\x02':
        rows.append(size_rule(data, offset, context, 8, 10, 20, 'CENTRAL_STORED_SIZE'))
        if len(data) < 46:
            add('CENTRAL_HEADER', None, '고정 헤더 46바이트 부족')
            return rows
        nlen, xlen, clen = struct.unpack_from('<HHH', data, 28)
        end = 46 + nlen + xlen + clen
        add('CENTRAL_HEADER_EXTENT', True if len(data) >= end else None,
            f'파일명·extra·주석 포함 {end}바이트 필요, 현재={len(data)}')
        # 주석은 없어도 extra 영역이 다 있으면 TLV 검사 가능.
        if len(data) >= 46 + nlen + xlen:
            extra = data[46+nlen:46+nlen+xlen]
            pos = 0
            ok = True
            while pos < len(extra):
                if len(extra) - pos < 4:
                    ok = False
                    break
                pos += 4 + int.from_bytes(extra[pos+2:pos+4], 'little')
                if pos > len(extra):
                    ok = False
                    break
            add('CENTRAL_EXTRA_LAYOUT', ok, '선언된 extra 영역 안의 하위 필드 길이 구조 검사')
    else:
        if len(data) < 22:
            add('EOCD_HEADER', None, 'EOCD 고정 22바이트 부족')
            return rows
        disk, start_disk, here, total, size, start, comment = struct.unpack_from('<4H2IH', data, 4)
        add('EOCD_EXTENT', True if len(data) >= 22 + comment else None,
            f'주석 포함 {22+comment}바이트 필요, 현재={len(data)}')
        if 0xffff in (disk, start_disk, here, total) or 0xffffffff in (size, start):
            add('EOCD_COUNTS', None, 'ZIP64 sentinel: 추가 레코드 필요')
        elif disk != 0 or start_disk != 0:
            add('EOCD_COUNTS', None, '다중 디스크 조건: 이 버전에서 비교 보류')
        else:
            add('EOCD_COUNTS', here == total, f'단일 디스크 항목 수 비교: {here} / {total}')
        add('ARCHIVE_LINKS', None, '전체 directory·local header·데이터 연결 검증은 수행하지 않음')
    return rows


def validate_block(block, fmt, offset=None):
    fmt = fmt.upper()
    if fmt not in ('PNG', 'ZIP'):
        raise ValueError('PNG 또는 ZIP을 지정하세요')
    check = png_chunk if fmt == 'PNG' else zip_record
    if offset is not None:
        return check(block, offset)
    if fmt == 'PNG':
        offsets = [i for i in range(max(0, len(block)-7)) if bytes(block[i+4:i+8]) in PNG_TYPES]
    else:
        signatures = (b'PK\x03\x04', b'PK\x01\x02', b'PK\x05\x06')
        offsets = [i for i in range(max(0, len(block)-3)) if block[i:i+4] in signatures]
    rows = []
    for i in offsets:
        rows.extend(check(block, i, 'scanned_candidate'))
    return rows or [result(fmt, None, 'STRUCTURE_EVIDENCE', 'UNKNOWN',
                          '검사 가능한 구조 후보 없음. 중간 데이터일 수 있어 포맷을 부정하지 않음', 'unanchored')]


def main():
    parser = argparse.ArgumentParser(description="단일 블록 PNG/ZIP 구조 후보 검사")
    parser.add_argument("file")
    parser.add_argument("--format", choices=["PNG", "ZIP"], required=True)
    parser.add_argument("--block-size", type=int, default=512)
    parser.add_argument("--block-index", type=int, default=0)
    parser.add_argument("--structure-offset", type=int, help="블록 내 구조 시작 위치를 알고 있을 때만 지정")
    args = parser.parse_args()
    if not 1 <= args.block_size <= 1048576 or args.block_index < 0:
        parser.error("block-size는 1~1048576, block-index는 0 이상")
    with open(args.file, "rb") as source:
        source.seek(args.block_index * args.block_size)
        block = source.read(args.block_size)
    if args.structure_offset is not None and not 0 <= args.structure_offset <= len(block):
        parser.error("structure-offset이 읽은 블록 범위를 벗어남")
    print(json.dumps({"block_index": args.block_index, "bytes_read": len(block),
                      "results": validate_block(block, args.format, args.structure_offset)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
