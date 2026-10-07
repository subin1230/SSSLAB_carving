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

### 512바이트 블록 실험 자동 실행

```sh
python3 block_demo.py
```

완전한 PNG와 ZIP 원본 파일을 생성하고, 512바이트씩 분할하여 위치와 SHA-256을 기록합니다.
정상 구조, 복사본의 필드 손상, 다음 블록까지 이어지는 구조의 6가지 규칙별 예상 결과를 비교합니다.
모두 일치하면 PASS 6개가 출력됩니다. 생성물은 `block-demo-output/`에 저장합니다.
`report.md`는 비교표, `results.json`은 세부 결과, `block-index.csv`는 원본 위치 목록입니다.
`sample.png`, `sample.zip`, `crossing.zip`은 원본이며 `case-1.bin`~`case-6.bin`은 검사 입력입니다.
재실행하면 이 전용 폴더의 같은 이름 생성물을 덮어씁니다.
PNG 경계 걸침은 같은 첫 블록의 IHDR 대신 IDAT를 검사하며, ZIP은 긴 경로명이 있는 정상 ZIP을 사용합니다.
위치가 알려진 통제 실험이므로 자동 후보 탐색 정확도나 실제 포렌식 매체의 성능을 입증하지 않습니다.

### 원하는 파일 검사

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

## FFC 예측 연동

`ffc_pipeline.py`에는 NumPy가 필요합니다. 원본 입력 파일은 읽기만 하며 결과는 기본적으로 이 코드 폴더의 `ffc-output/실행시간/`에 저장합니다.

```sh
python3 ffc_pipeline.py \
  --npz /home/yurim/tesserae_fifty/512_1/test.npz \
  --meta /home/yurim/tesserae_fifty/512_1/test_meta.csv \
  --predictions /home/yurim/ffc_results/B_fiftyRT_512/test_predictions.csv \
  --limit 100
```

- NPZ의 x는 uint8 (N,512) 또는 (N,4096), y는 정수 (N,)이어야 합니다. x 전체를 메모리에 읽으므로 512바이트 696,320개는 배열만 약 340MiB이며 ID 목록 등 추가 메모리도 필요합니다.
- 모든 행의 CSV ID·원본 파일 ID·정답, meta row·NPZ y 라벨을 검사합니다. 중복 ID와 행 수·순서 불일치는 중단합니다. 이 버전은 동일 순서 export만 허용하며 잘못된 순서를 추측해서 연결하지 않습니다.
- NPZ 자체에는 확인한 범위에서 ID가 없으므로 이 검사는 NPZ/meta의 생성 과정상 순서 보장을 대체하지 않습니다. 같은 라벨 내 순서 변경이나 원본 바이트 변환 여부는 별도 확인해야 합니다.
- predicted_type이 png/zip인 후보만 검사합니다. masked 예측은 사용하지 않습니다. 정답과 원본 offset을 검사에 전달하지 않습니다.
- 기본 100개는 등장 순서상 앞의 후보로, 대표 표본이나 성능 평가가 아닌 연결 확인용입니다. 전체 실행은 `--limit 0`입니다.
- `blocks.csv`: 블록별 규칙 상태 존재 여부. `evidence.jsonl`: 규칙별 증거와 원본 메타데이터. `summary.json`: 후보 수·검사 수·입력 경로.
- has_invalid_rule은 구조 후보의 위반 증거가 있다는 뜻입니다. 블록 불가능성이나 오탐 제거 성공으로 집계하지 않으며 어떤 후보도 자동 제거하지 않습니다.
- B/PT/RT 등의 학습 조건, data_representation의 의미, NPZ의 원본 바이트 보존 여부는 아직 확인이 필요합니다.

## v2: 고정 데이터에서 검사 범위 확장

기존 구현은 `validator_v1.py`에 그대로 보존합니다. `validator.py`는 v2이며 v1의 기본 검사를 재사용합니다.
FFC 실행 시 `--validator-version v1` 또는 `--validator-version v2`로 선택합니다(기본 v2).
이전 v1 summary에는 version이 없지만 새 실행에는 `validator_version`, `rule_counts`, `per_format`을 기록합니다.

추가 규칙:
- PNG: 알려진 23종 청크 이름으로 탐색 확대. 모든 영문 4바이트를 청크로 간주하지 않습니다.
- PNG: PLTE 길이 3~768, 3의 배수 검사. cHRM/gAMA/sRGB/pHYs/tIME/acTL/fcTL 고정 길이 검사.
- PNG: IHDR의 width/height/depth+color/compression/filter/interlace를 확보된 필드별로 검사. CRC 부족은 필드 검사 중단 사유가 아닙니다.
- ZIP: 로컬 헤더가 30바이트 미만이거나 파일명이 잘려도 크기 필드까지 확보되면 제한된 비압축 크기 비교.
- ZIP: Central Directory 파일 헤더(46바이트+가변 필드) 후보의 범위·extra 길이 구조·제한된 비압축 크기 비교.
- ZIP: EOCD(22바이트+주석) 범위와 단일 디스크 항목 수 일치. ZIP64 sentinel과 다중 디스크는 UNKNOWN으로 보류.

근거: PNG W3C Third Edition 11.2.1, 11.2.2, 11.3의 해당 청크 정의 및 PKWARE APPNOTE 4.3.7, 4.3.12, 4.3.16.
- https://www.w3.org/TR/png-3/
- https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT

범위: EOCD/central 검사는 레코드 내부만 검사하며 실제 전체 ZIP 연결을 보장하지 않습니다. 부분 구조가 없으면 계속 UNKNOWN입니다.
명시적 구조 시작 위치로 검사하지 않은 탐색 후보는 우연히 데이터 내부에 나타날 수 있습니다. INVALID를 포맷 기각으로 자동 승격하지 않습니다.
CRC 일치나 ZIP 구조가 다른 포맷 라벨에서 발견돼도 내장 리소스/컨테이너일 수 있으므로 라벨과 바이트 구조를 구분해야 합니다.
현재 같은 테스트셋의 결과를 보고 규칙을 개선 중이므로 이후 결과는 탐색적 개발 결과로 기록합니다. 독립 평가 없이 일반화 성능으로 보고하지 않습니다.
서버 데이터는 로컬에서 접근하지 못했으므로 v2 실데이터 개선 효과는 서버 재실행 후 확인합니다.

## 정답 PNG/ZIP 진단 모드

기존 FFC 후보 실험은 `--mode prediction`(기본값), 정답 PNG/ZIP의 검사 가능 범위 진단은 `--mode ground-truth`입니다.
후자는 정답이 PNG/ZIP인 블록을 선택하고 정답 포맷 규칙을 적용합니다. FFC가 다른 포맷으로 예측한 블록도 포함됩니다.
이 결과를 FFC 후보 제거 성능으로 해석하지 마세요. 데이터 대응 확인과 예측별 비교를 위해 같은 예측 CSV를 계속 받습니다.
CSV/JSONL에는 selection_mode, validation_format, 원래 predicted_type을 구분해 저장합니다. ffc_correct는 모드와 무관하게 원래 예측과 정답의 일치 여부입니다.
summary에는 selection_mode와 uses_ground_truth_for_validation이 기록됩니다. per_format의 no_structure_candidate는 구조 단서 없음, unknown_only는 모든 규칙 UNKNOWN, has_valid_rule/has_invalid_rule은 해당 판정이 하나 이상인 블록 수입니다.

```sh
python3 ffc_pipeline.py \
  --npz /home/yurim/tesserae_fifty/512_1/test.npz \
  --meta /home/yurim/tesserae_fifty/512_1/test_meta.csv \
  --predictions /home/yurim/ffc_results/B_fiftyRT_512/test_predictions.csv \
  --validator-version v2 --mode ground-truth --limit 0
```

## v3: 단일 블록 경계 근거 분리

v1/v2는 각각 보존하며 기본값은 v3입니다. 두 블록을 연결하거나 원본 파일 offset을 검사에 사용하지 않습니다.

- `status`: 이전과 같은 조건부 규칙 검사 결과. 문자열 후보의 위반도 삭제하지 않습니다.
- `boundary_evidence`: PNG 시그니처에서 CRC 확인하며 따라온 경계(png_signature_chain), 완전한 비어있지 않은 청크의 CRC 일치(crc_consistent_chunk), 그 청크 직후 위치(after_crc_chunk), 단순 문자열/시그니처 일치(signature_only), CRC 확인된 데이터 내부(inside_crc_checked_payload), 단서 없음(no_candidate), 호출자가 지정한 위치(caller_assumed).
- `evidence_status`: 경계 근거가 부족하거나 다른 CRC 확인 청크의 내용 안에 있는 후보는 UNKNOWN. 시그니처·CRC 기반 경계 근거가 있는 후보는 원래 규칙 판정을 유지합니다. 이는 휴리스틱 증거 분류로, 인증이나 포맷 전체 판정이 아닙니다.
- PNG 내부의 경계는 길이와 CRC를 이용해 추적합니다. 미등록 청크도 이렇게 얻은 위치에서 일반 구조 검사할 수 있습니다. IEND만 단독 발견하면 고정 패턴 증거로만 기록합니다.
- ZIP은 이번 버전에 추가 경계 확립 로직을 구현하지 않았습니다. 자동 탐색은 signature_only로 보수적으로 보류하며 기존 원시 규칙 결과는 남습니다.
- 명시적 offset은 caller_assumed입니다. 사용자가 제공한 가정이며 확인된 경계라는 뜻은 아닙니다.
- 기존 summary의 has_invalid_rule/invalid_evidence_on_wrong 등은 계속 원시 status 기준입니다. 새 boundary_supported_invalid_blocks와 unanchored_invalid_blocks, evidence_unknown_only_blocks를 함께 봐야 합니다. boundary_counts/evidence_counts는 규칙 행 수이며 블록 수와 다릅니다.
- 경계 근거가 있어도 원본의 손상·내장 리소스·CRC 충돌 가능성을 배제하지 않으므로 FFC 후보는 자동 기각하지 않습니다. UNKNOWN 증가를 성능 향상이나 오탐 제거 성공으로 보고하지 않습니다.

실행 예: 기존 명령에서 `--validator-version v3` 사용. 먼저 `--mode ground-truth`로 문제 사례를 점검하고, 동일 버전 `--mode prediction` 결과를 별도 수집하세요.

### v4: 단일 블록 내부 값·항목 데이터 검사

`--validator-version v4`로 선택한다. 비교 재현을 위해 기본값과 v1~v3는 유지한다.

- PNG: sRGB rendering intent(0~3), pHYs 단위(0/1), 양수 gAMA,
  tIME 월·일·시·분·초 범위를 추가 검사한다. CRC가 맞아도 필드가 명세를
  위반하면 INVALID가 가능하다. 월별 실제 날짜 유효성 검사는 포함하지 않는다.
- ZIP: 완전한 로컬 헤더와 항목 데이터가 단일 블록에 있으면 stored/DEFLATE
  해제 크기와 데이터 CRC32를 검사한다. DEFLATE 종료 및 선언 구간도 확인한다.
  해제 출력은 1 MiB로 제한하며 초과는 UNKNOWN이다.
- ZIP 경계 근거: 같은 블록에 완전한 일반 단일 디스크 중앙 디렉터리와 EOCD가
  있으면 선언된 상대 오프셋으로 로컬 헤더를 찾는다. 파일명도 일치해야 한다.
  이때 로컬/중앙 flags·method·CRC·size 모순 및 데이터 오류는
  `zip_directory_reference`에 기반한 조건부 증거로 기록한다.
  중앙 디렉터리 순서와 로컬 파일 순서가 같다고 가정하지 않는다.
- 암호화, descriptor, ZIP64, 미지원 압축 방식은 payload 검사를 보류한다.
  구조가 잘리거나 디렉터리 연결을 확인할 수 없으면 자동 경계 확정을 하지 않는다.
- `status`는 가정한 구조 후보의 규칙 결과이고 `evidence_status`는 경계 근거까지
  반영한 결과다. FFC 후보 자동 기각은 계속 수행하지 않는다.
  `invalid_evidence_on_wrong` 등 기존 raw 집계를 오탐 제거 성공률로 사용하면 안 된다.

근거: [W3C PNG 명세](https://www.w3.org/TR/png/),
[PKWARE APPNOTE](https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT).

실제 512바이트 데이터는 로컬 헤더조차 희소했다. 추가 조건이 성립하지 않으면
v4에서도 UNKNOWN 또는 근거 있는 INVALID 0건이 유지될 수 있다.
새 규칙의 단위 테스트 성공은 실제 데이터셋에서의 제거 효과를 뜻하지 않는다.

### 단서 적용 범위 조사 (검사 규칙 변경 없음)

### v5: ZIP 로컬 항목 사이의 조건부 위치 관계

`--validator-version v5`는 v4 검사를 유지하고 `LOCAL_ENTRY_ADJACENCY`를 추가한다.
완전한 로컬 헤더의 이름·extra 길이와 압축 크기로 계산한 항목 끝에
다음 완전한 로컬 헤더가 있으면 원시 status VALID를 기록한다.
다음 항목 데이터는 블록 밖에 있어도 되지만 다음 헤더는 모두 확보해야 한다.
암호화·descriptor·ZIP64 크기·미지원 압축 방식은 제외한다.
다음 헤더가 없다는 사실은 INVALID로 처리하지 않는다.
두 후보의 위치 일치는 독립적인 경계 확립이 아니므로 이 규칙의
evidence_status는 UNKNOWN으로 유지한다. 자동 기각과 기존 경계 판정은 바꾸지 않는다.
따라서 원시 VALID 증가를 정확도 향상으로 해석하지 않는다.

### 단서 조사 실행

기존 v3/v4 실행을 `python3 audit_clues.py --run /path/to/run`으로 분석한다.
실행 summary에 기록된 NPZ를 읽으므로 동일 데이터 경로가 필요하다.
`clue-output/날짜/`에 report.md, clue_summary.json, examples.jsonl을 저장한다.
블록 단위 범주, 후보 단위 선언 범위, 정답/예측별 분포를 분리한다.
예시는 포맷·범주·정답별 처음 3개이며 주변 최대 64바이트 hex/ASCII를 포함한다.
경계 근거 있음은 검사 완료나 파일 포맷 확정이 아니며, 선언 범위가 블록 밖이라는
사실도 실제 구조 경계 걸침을 입증하지 않는다. test 실행 분석은 탐색적 결과로만
사용하고 규칙 개발용 train/val 실행과 최종 평가를 구분한다.

PNG 선언 범위 초과 후보를 자세히 조사하려면
`python3 analyze_png_candidates.py --run /path/to/v4-run`을 실행한다.
블록의 가장 강한 단서가 `declared_extent_outside_block`인 PNG 블록을 모두 선택하고,
그 안의 해당 후보를 candidates.jsonl에 저장한다. 청크 종류·선언 길이·정답별 집계와
최대 12개 주변 바이트 예시를 출력한다. printable 길이 바이트는 문자열 오인 점검용이며
실제 구조 여부 또는 위반 판정 규칙이 아니다. 서버 실데이터 결과는 실행 후 확인해야 한다.

### 헤더 없는 블록의 통계적 특징 탐색

`python3 profile_block_features.py --data-dir /home/yurim/tesserae_fifty/512_1`
은 train/val에서 클래스별 최대 256개를 고정 seed로 선택한다. x.npy를 순차 배치로
읽어 전체 2.7GB 배열을 메모리에 올리지 않지만, 파일 전체를 읽는 I/O는 필요하다.
NPZ/메타 row·label을 확인하고 선택 블록의 ID와 특징을 저장한다.
엔트로피, 출력 가능 문자, 0바이트, 상위 바이트, 인접 동일 바이트, 인접 차이를
조사한다. train PNG/ZIP의 특징별 0.5~99.5 백분위 범위를 val에 적용한다.
특징 결합이나 자동 기각은 하지 않는다. 범위 밖은 명세 위반이 아니라 통계적
비전형성이므로 INVALID로 변환하면 안 된다. FFC val 예측을 입력하지 않으므로
출력은 FFC 조건부 제거율이 아니다. 원본 파일 중복은 선택 표본에서만 확인한다.
이 실험은 구조 규칙을 보완할 데이터 기반 경로의 타당성 조사이며, 유효성이나
성과를 보장하지 않는다. 기존 test는 사용하지 않는다.
