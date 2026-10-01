"""python3 demo.py: 파일 설치 없이 여섯 가지 결과를 확인."""
import io
import struct
import zipfile
import zlib
from validator import validate_block


def examples():
    body = b"IHDR" + struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0)
    png = struct.pack(">I", 13) + body + struct.pack(">I", zlib.crc32(body))
    damaged = bytearray(png)
    damaged[-1] ^= 1
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as z:
        z.writestr("hello.txt", b"hello")
    zipdata = archive.getvalue()
    badzip = bytearray(zipdata)
    badzip[18:22] = struct.pack("<I", 9)
    return [("PNG 정상 청크", "PNG", png), ("PNG CRC 손상", "PNG", bytes(damaged)),
            ("PNG 잘린 청크", "PNG", png[:15]), ("ZIP 정상 헤더", "ZIP", zipdata),
            ("ZIP 비압축 크기 모순", "ZIP", bytes(badzip)), ("ZIP 잘린 헤더", "ZIP", zipdata[:20])]


if __name__ == "__main__":
    for title, fmt, data in examples():
        print(f"\n[{title}]")
        for row in validate_block(data, fmt):
            print(f"  {row['rule']}: {row['status']} — {row['reason']}")
