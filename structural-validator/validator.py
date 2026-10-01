"""단일 블록의 구조 후보를 검사한다. 결과는 파일 전체 판정이 아니다."""
import argparse
import json
import struct
import zlib


def result(fmt, offset, rule, status, reason, context):
    return dict(format=fmt, offset=offset, rule=rule, status=status,
                reason=reason, context=context, scope="structure_candidate")


def png_chunk(block, offset=0, context="assumed_boundary"):
    """offset이 청크 시작이라는 가정 아래 검사. CRC는 Type+Data 대상."""
    rows = []
    def add(rule, status, reason):
        rows.append(result("PNG", offset, rule, status, reason, context))
    if not 0 <= offset <= len(block):
        raise ValueError("offset은 블록 범위 안이어야 합니다")
    data = block[offset:]
    if len(data) < 8:
        add("CHUNK_HEADER", "UNKNOWN", "Length와 Type을 읽을 8바이트가 부족함")
        return rows
    length = int.from_bytes(data[:4], "big")
    kind = data[4:8]
    if length > 0x7fffffff:
        add("CHUNK_LENGTH", "INVALID", "PNG 청크 길이의 최대값 2^31-1 초과")
        return rows
    if not all(65 <= b <= 90 or 97 <= b <= 122 for b in kind) or not 65 <= kind[2] <= 90:
        add("CHUNK_TYPE", "INVALID", "Type은 영문 4바이트이며 세 번째 문자는 대문자여야 함")
        return rows
    add("CHUNK_HEADER", "VALID", "청크 길이 범위와 타입 문자 규칙 통과")
    if kind in (b"IHDR", b"IEND"):
        expected = 13 if kind == b"IHDR" else 0
        add("FIXED_LENGTH", "VALID" if length == expected else "INVALID",
            f"{kind.decode()} 데이터 길이: {length}, 요구값: {expected}")
    if len(data) < length + 12:
        add("CHUNK_CRC", "UNKNOWN", f"청크 전체 {length + 12}바이트 필요, 현재 {len(data)}바이트")
        return rows
    payload = data[8:8 + length]
    stored = int.from_bytes(data[8 + length:12 + length], "big")
    computed = zlib.crc32(data[4:8 + length]) & 0xffffffff
    add("CHUNK_CRC", "VALID" if stored == computed else "INVALID",
        f"저장 CRC={stored:08x}, 계산 CRC={computed:08x}")
    if kind == b"IHDR" and length == 13:
        width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
        depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
        ok = (0 < width <= 0x7fffffff and 0 < height <= 0x7fffffff
              and depth in depths.get(color, set()) and compression == 0
              and filtering == 0 and interlace in (0, 1))
        add("IHDR_FIELDS", "VALID" if ok else "INVALID",
            f"크기={width}x{height}, 비트깊이={depth}, 색상유형={color}, 압축={compression}, 필터={filtering}, 인터레이스={interlace}")
    return rows


def zip_header(block, offset=0, context="assumed_boundary"):
    """Local File Header만 검사. payload CRC, 전체 ZIP 검증은 하지 않음."""
    rows = []
    def add(rule, status, reason):
        rows.append(result("ZIP", offset, rule, status, reason, context))
    if not 0 <= offset <= len(block):
        raise ValueError("offset은 블록 범위 안이어야 합니다")
    data = block[offset:]
    if len(data) < 4:
        add("LOCAL_SIGNATURE", "UNKNOWN", "시그니처 4바이트 부족")
        return rows
    if data[:4] != b"PK\x03\x04":
        add("LOCAL_SIGNATURE", "INVALID", "지정한 위치가 Local File Header 시그니처와 다름")
        return rows
    if len(data) < 30:
        add("LOCAL_HEADER", "UNKNOWN", "고정 헤더 30바이트 부족")
        return rows
    _, version, flags, method, _, _, crc, csize, usize, nlen, xlen = struct.unpack("<4s5H3I2H", data[:30])
    end = 30 + nlen + xlen
    add("LOCAL_FIXED_FIELDS", "VALID", f"고정 필드 읽기 성공: version={version}, method={method}, filename_length={nlen}, extra_length={xlen}")
    if len(data) < end:
        add("LOCAL_HEADER_EXTENT", "UNKNOWN", f"가변 필드 포함 {end}바이트 필요, 현재 {len(data)}바이트")
        return rows
    add("LOCAL_HEADER_EXTENT", "VALID", "파일명과 추가 필드가 블록 안에 포함됨")
    extra = data[30 + nlen:end]
    pos = 0
    while pos < len(extra):
        if len(extra) - pos < 4:
            add("EXTRA_FIELD_LAYOUT", "INVALID", "완전히 확보된 Extra Field 내부에 하위 헤더가 잘림")
            return rows
        size = int.from_bytes(extra[pos + 2:pos + 4], "little")
        pos += 4 + size
        if pos > len(extra):
            add("EXTRA_FIELD_LAYOUT", "INVALID", "하위 필드 길이가 선언된 Extra Field 영역을 초과함")
            return rows
    add("EXTRA_FIELD_LAYOUT", "VALID", "추가 필드의 길이 구조 통과 (내용별 의미 검증은 제외)")
    if flags & 8:
        reason = "Data Descriptor 사용: Local Header만으로 최종 크기·CRC 확정 불가"
    elif flags & (1 | 64 | 8192):
        reason = "암호화 관련 플래그 사용: 이 버전에서는 데이터 검증 미지원"
    elif csize == 0xffffffff or usize == 0xffffffff:
        reason = "ZIP64 크기 해석은 이 버전에서 미지원"
    elif method not in (0, 8):
        reason = f"압축 방식 {method}의 데이터 검증은 미지원"
    else:
        if method == 0:
            add("STORED_SIZE", "VALID" if csize == usize else "INVALID",
                f"비압축·비암호화 entry의 크기는 같아야 함: compressed={csize}, uncompressed={usize}")
        reason = "압축 데이터가 블록 밖으로 이어짐" if len(data) < end + csize else "헤더 검사만 수행함. 데이터 압축 해제·CRC 검증은 미구현"
    add("ENTRY_DATA_CRC", "UNKNOWN", reason)
    return rows


def validate_block(block, fmt, offset=None):
    """자동 탐색은 후보의 증거만 반환. 후보 INVALID를 블록 INVALID로 집계하지 않는다."""
    fmt = fmt.upper()
    if fmt not in ("PNG", "ZIP"):
        raise ValueError("PNG 또는 ZIP을 지정하세요")
    check = png_chunk if fmt == "PNG" else zip_header
    if offset is not None:
        return check(block, offset)
    rows = []
    if fmt == "ZIP":
        offsets = [i for i in range(len(block)) if block.startswith(b"PK\x03\x04", i)]
    else:
        # 무작위 바이트의 오탐을 줄이기 위해 자동 탐색은 핵심 4개 청크로 제한.
        offsets = [i for i in range(max(0, len(block) - 7))
                   if block[i + 4:i + 8] in (b"IHDR", b"PLTE", b"IDAT", b"IEND")]
    for i in offsets:
        rows.extend(check(block, i, "scanned_candidate"))
    return rows or [result(fmt, None, "STRUCTURE_EVIDENCE", "UNKNOWN",
                          "검사 가능한 구조 후보 없음. 중간 데이터일 수 있어 포맷을 부정하지 않음", "unanchored")]


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
