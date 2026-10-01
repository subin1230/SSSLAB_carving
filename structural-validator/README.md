# PNG / ZIP 단일 블록 Structural Validator

Python 3 표준 라이브러리만 사용합니다. 추가 패키지 설치는 없습니다.
VS Code에서 이 폴더를 열고 Terminal → New Terminal에서 실행하세요.

## 먼저 실행

```sh
python3 demo.py
python3 -m unittest -v
```

demo.py는 PNG 정상/CRC 손상/잘림과 ZIP 정상/크기 모순/잘림을 메모리에서 만들어 검사합니다.
ZIP 정상 헤더에서도 ENTRY_DATA_CRC는 UNKNOWN입니다. 헤더 검사와 데이터 검사는 다른 규칙입니다.

## 실제 파일에서 블록 하나 읽기

```sh
python3 validator.py /절대/경로/example.png --format PNG --block-size 512 --block-index 0
python3 validator.py /절대/경로/example.zip --format ZIP --block-size 512 --block-index 1
```

파일 전체를 검증하지 않고 지정한 블록만 읽습니다. 마지막 블록을 0으로 채우지 않습니다.
읽을 데이터가 없거나 식별 가능한 구조가 없으면 UNKNOWN입니다.
--structure-offset 8 옵션은 블록 내부 8바이트 위치가 구조 시작이라고 **가정**하고 검사합니다.
PNG에서는 시그니처 위치가 아니라 청크 Length 시작 위치를 지정합니다.

## 코드 읽는 순서

1. validator.py의 result(): 공통 결과 형식
2. png_chunk(): PNG 청크 헤더, 고정 길이, CRC, IHDR 필드
3. zip_header(): ZIP Local File Header와 가변 필드 길이
4. validate_block(): 블록 안의 구조 후보 찾기
5. demo.py: 예제 생성과 결과 출력

## 결과 해석

format / offset / rule / status / reason을 반환합니다. offset은 0부터 세는 블록 내 구조 시작 위치입니다.
추가 context는 assumed_boundary(호출자가 가정한 시작), scanned_candidate(검색 후보), unanchored(단서 없음)입니다.
scope는 structure_candidate이며 블록 전체 판정을 의미하지 않습니다.

- VALID: 해당 위치의 해당 규칙만 통과했습니다. 전체 파일 정상/원본 일치/동일 파일 소속을 보장하지 않습니다.
- INVALID: 그 위치가 해당 구조라는 가정 아래 명시적인 모순이 있습니다.
- UNKNOWN: 정보 부족 또는 구현 범위 밖입니다.

자동 검색된 후보는 데이터 안에 우연히 나타날 수도 있습니다. 후보 하나의 INVALID를 FFC 예측 기각으로 사용하지 마세요.
같은 후보에서 헤더 VALID, CRC UNKNOWN처럼 결과가 함께 나올 수 있습니다. 합쳐서 하나의 VALID로 만들지 않습니다.

## 구현 범위와 규칙

| 규칙 | VALID | INVALID | UNKNOWN |
|---|---|---|---|
| PNG CHUNK_HEADER/TYPE/LENGTH | 길이 범위 및 타입 문자 통과 | 명세상 불가능한 길이/타입 | 8바이트 부족 |
| PNG FIXED_LENGTH | IHDR=13, IEND=0 | 고정 길이 위반 | 청크 헤더를 못 읽음 |
| PNG CHUNK_CRC | Type+Data의 CRC 일치 | 완전한 청크의 CRC 불일치 | 청크 끝까지 없음 |
| PNG IHDR_FIELDS | 크기 및 색상/깊이 조합 등 정상 | 금지된 값 | 전체 청크 부족 |
| ZIP LOCAL_HEADER_EXTENT | 고정/가변 헤더 확보 | 이 규칙은 부족만으로 INVALID를 내지 않음 | 블록 경계에서 잘림 |
| ZIP EXTRA_FIELD_LAYOUT | 하위 필드 길이 통과 | 확보한 extra 영역 내부 길이 모순 | extra 영역 자체 부족 |
| ZIP STORED_SIZE | 비압축·비암호화·크기 확정 entry의 크기가 동일 | 해당 조건에서 크기 불일치 | Descriptor/암호화/ZIP64 등은 검사 보류 |
| ZIP ENTRY_DATA_CRC | 미구현 | 미구현 | 항상 UNKNOWN, 이유 반환 |

PNG 자동 검색은 IHDR/PLTE/IDAT/IEND만 찾습니다. 다른 청크도 알려진 offset으로 일반 길이/CRC 검사는 가능하나 의미 검증은 제외합니다.
PNG 시그니처·전체 청크 순서·PLTE 내용·IDAT 압축 해제는 미구현입니다.
ZIP은 Local File Header만 대상입니다. 모든 압축 방식/버전/플래그의 의미 검증, ZIP64, 암호화, Central Directory, EOCD, 압축 해제와 entry CRC는 미구현입니다.
ZIP LOCAL_FIXED_FIELDS의 VALID는 필드 읽기 성공만 뜻하며 메타데이터 전체의 적법성을 뜻하지 않습니다.
지금은 학습용 프로토타입이며 실제 포렌식 데이터 정확도 평가는 아직 수행하지 않았습니다.

## 근거

- PNG: https://www.w3.org/TR/png-3/ — Chunk layout, Chunk naming conventions, CRC, IHDR, IEND 절
- ZIP: https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT — 4.3.7 Local File Header, 4.3.9 Data Descriptor, 4.5 Extra Fields

다음 확장에서는 별도 증거에 기반한 블록 판정 정책과 실제 데이터의 오탐/미탐 평가를 추가하세요.
