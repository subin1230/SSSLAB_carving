"""python3 block_demo.py: 실제 파일에서 추출한 블록의 규칙별 예상/실제 비교."""
import csv
import hashlib
import io
import json
from pathlib import Path
import random
import struct
import zipfile
import zlib

from validator import validate_block

OUT = Path(__file__).resolve().parent / 'block-demo-output'


def chunk(kind, data):
    return (struct.pack('>I', len(data)) + kind + data
            + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))


def make_sources():
    rng = random.Random(42)
    pixels = bytes(rng.randrange(256) for _ in range(64 * 64 * 3))
    raw = b''.join(b'\0' + pixels[i:i + 192] for i in range(0, len(pixels), 192))
    packed = zlib.compress(raw)
    assert zlib.decompress(packed) == raw
    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 64, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', packed) + chunk(b'IEND', b''))
    sources = {'sample.png': png}
    # 짧은 이름의 헤더는 블록에 들어가고, 긴 경로의 헤더는 경계를 넘는다.
    names = {'sample.zip': 'hello.txt',
             'crossing.zip': '/'.join(['a' * 100] * 5) + '/hello.txt'}
    payload = b'hello from block demo\n' * 100
    for filename, entry in names.items():
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_STORED) as archive:
            info = zipfile.ZipInfo(entry, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, payload)
        data = memory.getvalue()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.testzip() is None
            assert archive.read(entry) == payload
        sources[filename] = data
    return sources


def main():
    OUT.mkdir(exist_ok=True)
    sources = make_sources()
    block_index = []
    for name, data in sources.items():
        (OUT / name).write_bytes(data)
        folder = OUT / (name + '.blocks')
        folder.mkdir(exist_ok=True)
        for index, start in enumerate(range(0, len(data), 512)):
            block = data[start:start + 512]
            path = folder / f'{index:04d}.bin'
            path.write_bytes(block)
            block_index.append(dict(source=name, block_index=index, source_offset=start,
                                    bytes=len(block), block_file=str(path.relative_to(OUT)),
                                    sha256=hashlib.sha256(block).hexdigest()))
    cases = [
        ('PNG 정상 IHDR', 'sample.png', 'PNG', 8, 'CHUNK_CRC', 'VALID', None,
         'IHDR 전체가 첫 블록에 포함되고 CRC가 일치'),
        ('PNG CRC 손상', 'sample.png', 'PNG', 8, 'CHUNK_CRC', 'INVALID', 'png_crc',
         '첫 블록의 32번 바이트(CRC 마지막 바이트)를 1비트 변경'),
        ('PNG 경계 걸침', 'sample.png', 'PNG', 33, 'CHUNK_CRC', 'UNKNOWN', None,
         '정상 IDAT 청크가 다음 블록까지 이어져 CRC 확인 불가'),
        ('ZIP 정상 헤더', 'sample.zip', 'ZIP', 0, 'STORED_SIZE', 'VALID', None,
         '비압축 파일의 두 크기 값이 동일'),
        ('ZIP 크기 모순', 'sample.zip', 'ZIP', 0, 'STORED_SIZE', 'INVALID', 'zip_size',
         '첫 블록 18~21번 바이트의 압축 크기를 원래 값보다 1 증가'),
        ('ZIP 경계 걸침', 'crossing.zip', 'ZIP', 0, 'LOCAL_HEADER_EXTENT', 'UNKNOWN', None,
         '정상 ZIP의 긴 경로명 때문에 로컬 헤더가 다음 블록까지 이어짐'),
    ]
    report = []
    for number, (title, source, fmt, offset, rule, expected, mutation, basis) in enumerate(cases, 1):
        block = bytearray(sources[source][:512])
        assert len(block) == 512
        if mutation == 'png_crc':
            block[32] ^= 1
        elif mutation == 'zip_size':
            size = int.from_bytes(block[18:22], 'little')
            block[18:22] = struct.pack('<I', size + 1)
        path = OUT / f'case-{number}.bin'
        path.write_bytes(block)
        rows = validate_block(bytes(block), fmt, offset)
        matches = [row for row in rows if row['rule'] == rule]
        actual = matches[0]['status'] if len(matches) == 1 else 'MISSING'
        passed = actual == expected
        report.append(dict(case=title, source=source, source_offset=0,
                           block_bytes=512, structure_offset=offset, block_file=path.name,
                           mutation=mutation, basis=basis, rule=rule, expected=expected,
                           actual=actual, passed=passed, results=rows))
        print(f"{'PASS' if passed else 'FAIL'} | {title} | {rule}: 예상={expected}, 실제={actual}")
    (OUT / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with (OUT / 'block-index.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=list(block_index[0]))
        writer.writeheader()
        writer.writerows(block_index)
    lines = ['# 512바이트 블록 실험', '',
             '정상 원본을 코드로 생성한 통제 실험입니다. 실제 포렌식 매체의 정확도 평가는 아닙니다.',
             '판정은 명시한 위치의 특정 규칙에 대한 결과이며 블록 전체 판정이 아닙니다.',
             'PNG는 IHDR과 IDAT 두 위치를 같은 첫 블록에서 각각 검사합니다.',
             '원본은 보존하고 손상은 case-2.bin과 case-5.bin 복사본에만 적용합니다.', '',
             '| 예시 | 원본 | 블록 내 위치 | 규칙 | 예상 | 실제 | 근거 |',
             '|---|---|---:|---|---|---|---|']
    for row in report:
        lines.append(f"| {row['case']} | {row['source']} | {row['structure_offset']} | {row['rule']} | {row['expected']} | {row['actual']} | {row['basis']} |")
    lines.extend(['', '모든 예시 블록은 512바이트입니다. 전체 파일 분할의 마지막 블록은 패딩하지 않습니다.',
                  '전체 분할 위치와 SHA-256은 block-index.csv, 세부 판정은 results.json에 있습니다.',
                  'ZIP 원본은 Python zipfile로 전체 항목의 CRC와 내용 일치를 검사했습니다.',
                  'PNG는 생성 시 IDAT 압축 해제 후 원래 픽셀 행과 일치하는지 확인했습니다.',
                  '이미지 뷰어에서도 sample.png를 열어 확인할 수 있습니다.',
                  'ZIP 데이터 CRC는 validator 자체에서는 여전히 UNKNOWN입니다.'])
    (OUT / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'\n결과 폴더: {OUT}')
    if not all(row['passed'] for row in report):
        raise SystemExit('예상과 다른 결과가 있습니다.')


if __name__ == '__main__':
    main()
