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

### 예측·정답에 독립적인 전체 블록 탐색

Android 리소스 단서의 탐색적 APK 후보 실험:

```sh
python3 evaluate_android_chunks.py --run /path/to/complete-all-formats-run
```

기존 run의 NPZ 전체를 다시 읽어 npTc/npOl/npLb를 라벨과 무관하게 탐색한다.
완전한 청크 CRC 및 npOl=24/npLb=16 길이, npTc=32+4*(x/y/color 개수)의
길이 관계를 만족한 고유 블록을 후보로 집계한다. 전체 Android 의미 검증은 아니다.
원본 라벨별 후보 수·고유 원본 파일 수·APK 후보 정밀도·APK 적용 범위를 출력한다.
FFC를 APK로 덮어쓸 경우의 회복/손실 수는 가상 집계이며 실제 재분류하지 않는다.
val 관찰에서 선택한 규칙이므로 결과는 탐색적이며 독립 평가가 필요하다.
Android 리소스는 다른 포맷 안에 포함될 수도 있어 APK 확정 근거가 아니다.
근거: https://android.googlesource.com/platform/frameworks/base/+/master/tools/aapt/Images.cpp
및 include/androidfw/ResourceTypes.h의 Res_png_9patch.

완료된 all-formats 실행은 다음 명령으로 구조와 원본 라벨의 차이를 조사한다.

```sh
python3 analyze_supported_structures.py --run /path/to/all-formats-run
```

summary가 참조하는 동일 NPZ를 읽고 경계 근거 반영 VALID가 있는 모든 블록을 분석한다.
`structure-analysis/날짜/report.md`는 구조/정답별 블록 수·후보 종류·메타데이터 키워드·
근거 있는 ZIP 후보의 파일명을 정리한다. `supported_details.jsonl`에는 해당 포맷의
모든 후보와 PNG IHDR, 텍스트 키워드, ZIP 로컬/중앙 파일명, 블록 hex를 기록한다.
텍스트 메타데이터는 완전한 청크에서만 추출하며 압축 메타데이터는 해제하지 않는다.
라벨은 사후 그룹화에만 사용한다. 파일명·키워드는 원본 포맷의 확정 근거가 아니다.
기존 탐색을 반복하거나 재분류하지 않는다.

`--mode all-formats --validator-version v5 --limit 0`은 NPZ의 모든 블록에
PNG와 ZIP 검사를 각각 적용한다. 정답·FFC 예측은 입력 대응 확인과 사후 집계에만
사용하며 검사할 포맷 선택이나 바이트 탐색에는 사용하지 않는다.
`--limit 100`은 전체 행 중 앞의 100블록 연결 확인용이다.
기존 prediction/ground-truth 모드와 기본값은 유지한다.

- blocks.csv: 검사한 모든 블록의 후보 포맷·근거 반영 VALID 포맷·정답·예측.
- evidence.jsonl: 어느 포맷이든 구조 후보가 발견된 블록만 상세 저장한다.
  두 포맷 결과를 함께 기록하며 기존 audit_clues.py 입력 형식과 다르다.
- summary.json: none/png/zip/png+zip 후보 범주와 근거 범주를 별도로 집계한다.
  per_truth_label에서 라벨별 적용 범위를 확인한다.
- supported는 경계 근거 반영 VALID 규칙이 하나 이상이라는 뜻이다.
  파일 전체 형식 확정이 아니며 INVALID 동시 존재도 배제하지 않는다.
  잘못된 FFC 예측에서 정답 포맷 근거가 발견된 수와, 예측과 다른 근거가
  발견된 수를 구분한다. 둘 모두 재분류 성공 수가 아니다.
- 자동 기각·재분류는 수행하지 않는다. PNG/ZIP 구조 탐색이며 전체 클래스 분류기가 아니다.
  모든 바이트를 두 번 검사하므로 기존 후보 선택 모드보다 시간이 더 걸릴 수 있다.

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

### 단일 블록 접근의 상한 점검 (블록 바이트를 읽지 않음)

규칙을 더 추가하기 전에 두 가지를 먼저 확인한다.

**1. FFC 오답이 같은 압축 계열 안의 혼동인가**

```sh
python3 confusion_families.py \
  --predictions /home/yurim/ffc_results/B_fiftyRT_512/val_predictions.csv
```

PNG/ZIP으로 잘못 예측된 블록(`wrong_predicted_as`)과 놓친 정답 블록(`missed_truth`)의
실제 포맷을 deflate / deflate_mixed / other_compressed 등 계열로 묶는다.
deflate 비율이 높으면 구조 단서가 없는 블록 대부분은 같은 압축기 출력이므로
단일 블록 구조 검증으로 구별할 정보가 거의 없다는 뜻이다(증명이 아닌 규모 추정).
계열 정의는 `FAMILIES`의 가정이며, 미지정 포맷은 경고로 출력하므로 `--family-map`으로 보완한다.

**2. 같은 파일의 연속 블록이 데이터셋에 있는가**

```sh
python3 count_adjacent_blocks.py --meta \
  /home/yurim/tesserae_fifty/512_1/train_meta.csv \
  /home/yurim/tesserae_fifty/512_1/val_meta.csv \
  /home/yurim/tesserae_fifty/512_1/test_meta.csv
```

`source_file_id`와 `block_index_in_file`(없으면 `byte_offset`)로 split별 연속 쌍 수,
이웃 블록이 있는 블록 비율, 첫 블록(헤더)을 가진 파일 수를 센다.
연속 쌍이 거의 없으면 블록 쌍 실험은 원본 파일에서 쌍 데이터셋을 새로 만들어야 한다.
`files_in_multiple_splits`가 0이 아니면 split이 파일 단위로 분리되지 않았으므로 별도 확인한다.
### ZIP 계열 연속 구간 파일럿

`extract_zip_pilot.py --root /data-18t/research/tesserae-dataset --cache /home/yurim/tesserae_cache`
은 val 16KB 그룹에서 JAR/APK/DOCX/PPTX/XLSX 각 최대 10개 파일의 첫 적격 그룹을
선택한다. 모든 선택 그룹의 파일 코드·타입·표현 플래그 일치 및 블록 인덱스 연속성을
검사한다. blocks.npy는 순서를 섞은 (N,512) 바이트이며 행 번호가 sample_id이다.
truth.csv는 원본 ID·위치·라벨을 저장하는 평가 전용 파일로 모델이나 grouping에
제공하면 안 된다. 편의 표본이며 전체 파일·대표 표본·독립 최종 평가가 아니다.
16KB 밖 ZIP 참조 대상은 누락될 수 있으므로 연결 대상 포함률을 별도로 평가한다.

### ZIP 연결 후보 생성과 정답 분리 평가

`audit_zip_pilot.py --pilot /path/to/pilot`은 정답 파일 ID·위치를 이용해 대상 위치에
ZIP 시그니처가 있는지 확인하는 oracle 진단이다. `matching_cross_block_relations`는
블록을 가로지르는 참조 사건 수이며, 고유 블록 쌍 수·예측 정확도·grouping 입력이 아니다.

`zip_pair_candidates.py`는 다음 단계로, 순서를 섞은 `blocks.npy`만 읽어 연결 후보를
먼저 저장하고, 별도 평가 명령에서만 `truth.csv`를 읽는다.

```sh
python3 zip_pair_candidates.py generate \
  --blocks /path/to/pilot/blocks.npy \
  --output /path/to/pilot/pair-candidates-v1
python3 zip_pair_candidates.py evaluate \
  --blocks /path/to/pilot/blocks.npy \
  --truth /path/to/pilot/truth.csv \
  --candidates /path/to/pilot/pair-candidates-v1
```

- `zip_next`: 로컬 헤더 길이·압축 크기로 다음 헤더가 나타날 **블록 내부 위치**를
  계산하고, 그 위치에 로컬/중앙 헤더 후보가 있는 모든 블록을 남긴다. 상대 거리도
  저장한다. 입력 행 순서를 원본 순서로 취급하지 않는다. 위치만 같아도 여러 파일의
  후보가 나올 수 있으므로 약한 단서이다.
- `cd_local`: 중앙 헤더와 로컬 헤더의 파일명 바이트·플래그·압축 방식·CRC·크기와
  선언 오프셋의 블록 내부 위치가 일치하는 후보를 찾는다. CRC는 두 필드의 비교이며
  압축 데이터 전체의 무결성 검사가 아니다. 공통 리소스를 담은 서로 다른 파일에도
  같은 값이 있으므로 이 관계도 같은 파일 확정 근거가 아니다.
- `eocd`: 중앙 디렉터리의 선언 시작·크기와 EOCD 위치의 정렬 관계를 확인하고,
  시작 위치에 중앙 헤더 후보가 있는 블록을 찾는다. 디렉터리 전체를 검증하지 않는다.

입력은 **원본 파일의 512바이트 경계에 맞춰 추출한 블록**이어야 한다. 중앙/EOCD
절대 오프셋은 아카이브 시작이 원본 파일의 0번 위치라고 가정한다. 내장 ZIP·SFX,
ZIP64·멀티디스크·암호화·data descriptor 등은 현재 지원 범위 밖이며, 지원하지 않는
조건은 다른 파일이라는 판정으로 바꾸지 않는다. 고정 헤더가 잘리면 제외하고,
`cd_local`은 파일명 전체까지 한 블록 안에 있어야 한다. 여러 블록을 먼저 정답 순서로
합치지 않는다. 현재 압축 방식은 stored/deflate만 후보로 사용한다.
필드 기준은 [PKWARE APPNOTE](https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT)
4.3.7·4.3.12·4.3.16을 참조한다.

`queries.jsonl`에는 후보가 없거나 여럿인 경우도 그대로 저장한다.
`candidate_summary.json`은 후보 생성 집계와 입력 바이트 SHA-256을 기록한다.
평가 시 바이트가 달라지면 중단한다. `evaluation.json`의 주요 지표는 다음과 같다.

| 지표 | 의미 |
|---|---|
| `exact_target_precision` | 모든 후보 관계 중 실제 참조 대상 블록에 연결한 비율 |
| `same_file_precision` | 모든 후보 관계 중 같은 원본 파일에 연결한 비율 |
| `singleton_exact_precision` | 후보가 하나뿐인 질의에서 실제 대상을 맞힌 비율 |
| `available_target_query_recall` | 지원하는 출발 질의 중 정답 대상 시그니처가 표본 안에 있는 경우, 그 대상을 후보에 포함한 비율 |
| `unique_unordered_pairs` | 같은 블록 쌍에서 발생한 여러 참조를 합친 쌍 수 |
| `same_file_unique_pairs` | 그중 같은 원본 파일의 쌍 수 |

가용 대상 recall은 모든 ZIP 참조의 recall이 아니다. 이 생성기는 oracle 진단보다
대상 헤더·파일명 조건이 엄격하므로 oracle 건수와 직접 같아질 필요도 없다.
출력 디렉터리와 평가 파일은 덮어쓰지 않는다. 아직 가중치를 정하거나 Leiden에
관계를 추가하지 않으며, 이 결과로 후보 모호성과 오연결부터 확인한다. 50개 파일의
편의 표본 결과를 일반화하지 말고 규칙을 고정한 뒤 별도 파일에서 평가해야 한다.

### ZIP 쌍의 실제 데이터 검사: 압축 해제·크기·CRC

`zip_pair_payload.py`는 Same-File 모델 없이 기존 ZIP 연결 후보를 더 검사한다.
`zip_next`가 바로 다음 512바이트 블록을 가리키고, 로컬 헤더가 선언한 항목 데이터
전체가 두 블록에 들어 있을 때만 실행한다. 후보 두 블록을 가정한 순서로 이어서
stored 데이터 또는 raw Deflate 스트림을 읽고, 비압축 크기와 계산한 CRC32를 확인한다.
CRC 필드끼리 비교하는 기존 `cd_local`과 달리 실제 내용으로 CRC를 계산한다.

```sh
python3 zip_pair_payload.py check \
  --blocks /path/to/pilot/blocks.npy \
  --candidates /path/to/pilot/pair-candidates-v1 \
  --output /path/to/pilot/payload-v1
python3 zip_pair_payload.py evaluate \
  --blocks /path/to/pilot/blocks.npy \
  --truth /path/to/pilot/truth.csv \
  --run /path/to/pilot/payload-v1
```

검사는 정답을 읽지 않는다. 중간 블록이 빠진 관계는 임의로 이어 붙이지 않고 UNKNOWN으로
남긴다. data descriptor, 암호화·미지원 플래그, ZIP64 등의 조건도 UNKNOWN이다.
데이터가 출발 블록 안에서 이미 끝나면 CRC 통과가 대상 블록을 지지하지 않으므로
UNKNOWN으로 남긴다. 비압축 데이터가 빈 경우에도 같은 원칙으로 통과 근거를 만들지 않는다.
출력 크기는 기본 8MiB로 제한하며, 자원 제한을 넘는 선언은 구조 위반으로 해석하지 않는다.

| 결과 | 의미 |
|---|---|
| PASS | 대상 블록의 데이터를 포함한 완전한 항목의 압축 해석·크기·CRC가 일치함 |
| FAIL | 로컬 헤더와 해당 연결 순서를 가정했을 때 데이터 해석·크기·CRC에 모순이 있음 |
| UNKNOWN | 완전한 데이터가 없거나 지원 범위·자원 범위 밖이거나 대상 블록을 검증하지 못함 |

세 결과 모두 파일 정체성을 확정하지 않는다. 같은 내용이 다른 원본 파일에 복사되어
있으면 서로 다른 파일의 연결도 PASS할 수 있다. FAIL은 **이 항목의 재구성 가설**에
대한 결과이며 두 블록이 같은 파일일 수 없다는 negative edge로 사용하지 않는다.
인식한 로컬 헤더 자체가 잘못된 가설인 경우도 있으므로 정답 연결 손실을 함께 평가한다.

동일한 원래 질의 집합에 대해 아래 세 후보 집합을 저장하고, 별도 평가에서 비교한다.

- `baseline`: 기존 연결 후보 전부.
- `keep_not_failed`: FAIL만 제외하고 PASS·UNKNOWN은 유지.
- `payload_passed`: PASS만 선택한 제한된 근거 집합. 다른 관계의 거짓을 의미하지 않음.

`payload_summary.json`은 적용 범위와 PASS/FAIL/UNKNOWN 원인을, `payload_evidence.jsonl`은
관계별 검사 근거를 저장한다. `comparison.json`에는 세 집합의 정밀도·조건부 대상 포함률·
고유 쌍 수와, FAIL로 제외된 **정확한 대상 관계 수** 및 **다른 파일 관계 수**가 기록된다.
통과 집합의 정밀도만 보지 말고 적용 건수, 정답 연결 손실, 같은 파일의 다른 위치 관계도
함께 확인한다. 입력 바이트와 저장된 후보 파일의 해시를 확인하고 기존 결과는 덮어쓰지 않는다.
서버 파일럿 결과는 아래와 같으며, 생성 ZIP을 이용한 로직 테스트와 구분한다.

CRC·ZIP 데이터 필드는 [PKWARE APPNOTE](https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT),
raw Deflate의 종료·잔여 데이터 확인은 [Python zlib 문서](https://docs.python.org/3/library/zlib.html)를
참조한다. 원래 구조를 확인할 데이터가 확보된 쌍에 대한 추가 검사이며 전체 ZIP 복구기는 아니다.

### 파일럿 결과와 통과·실패 사례 사후 분석

2026-10-12 서버의 50개 파일·1,600개 블록 편의 표본(`payload-v1`)에서 보고된
`zip_next` 결과이다. 건수는 참조 관계 사건 수이며 고유 블록 쌍 수가 아니다.

| 후보 집합 | 관계 수 | 정확한 대상 | 다른 파일 | 정확한 대상 비율 |
|---|---:|---:|---:|---:|
| 기존 후보 | 1,178 | 453 | 709 | 38.46% |
| FAIL만 제외 | 903 | 453 | 441 | 50.17% |
| PASS만 선택 | 221 | 218 | 3 | 98.64% |

FAIL 275건은 다른 파일 관계 268건과 같은 파일의 잘못된 위치 관계 7건이었다.
따라서 정확한 참조 대상 453건은 유지했지만, 같은 파일 관계를 모두 유지한 것은 아니다.
PASS만 선택하면 정확한 대상 관계도 453건에서 218건으로 줄어든다.
통과 집합의 98.64%를 전체 연결 정확도나 grouping 성능으로 보고하면 안 된다.
`cd_local`·`eocd`는 이번 payload 검사에서 모두 UNKNOWN으로 유지됐다.

`audit_zip_payload.py`는 다른 파일인데 PASS한 사례와 같은 파일인데 FAIL한 사례를
정답 위치에 비추어 분석한다. 기존 블록·후보 파일 해시를 확인하고, 모든 관계의
payload 검사를 재실행하여 저장된 근거와 일치하는지 확인한 뒤 보고서를 만든다.
기존 `check`·후보 생성에는 정답이 들어가지 않으며, **이 분석에서만** `truth.csv`를 읽는다.

```sh
python3 audit_zip_payload.py \
  --blocks /path/to/pilot/blocks.npy \
  --truth /path/to/pilot/truth.csv \
  --run /path/to/pilot/payload-v1 \
  --output /path/to/pilot/payload-audit-v1
```

- `audit_summary.json`: PASS/FAIL/UNKNOWN별 관계 수·고유 무방향 쌍 수·관련 블록 수·
  원본 파일 수와 출발 포맷별 집계. 서로 다른 범주의 블록·파일 수는 중복될 수 있다.
- `cross_file_pass.jsonl`: 다른 원본 파일을 연결했는데 PASS한 모든 사례.
  실제 정답 대상이 표본에 있으면 대상 블록 전체, 사용된 대상 payload 구간,
  재구성한 압축 데이터, 압축 해제한 데이터를 각각 비교한다. 비교는 실제 바이트
  equality로 수행하며 SHA-256도 기록한다. payload 전체나 원본 파일은 새로 저장하지 않는다.
- `same_file_wrong_target_fail.jsonl`: 같은 파일의 다른 위치를 연결해서 FAIL한 사례.
  선언된 정답 위치와의 블록 차이, 실제 정답 대상에 대한 재검사 결과를 기록한다.
- `exact_target_fail.jsonl`: 정확한 대상인데 FAIL한 사례. 0건이어도 빈 파일을 저장한다.

다른 파일 PASS의 `comparison` 해석은 다음과 같다.

| 값 | 확인한 내용 |
|---|---|
| `IDENTICAL_TARGET_BLOCK` | 정답 대상과 후보의 512바이트 전체가 같음 |
| `IDENTICAL_RECONSTRUCTED_COMPRESSED_PAYLOAD` | 대상 블록 전체는 다르지만 재구성한 압축 데이터는 같음 |
| `IDENTICAL_DECODED_PAYLOAD_ONLY` | 압축 데이터는 다르지만 압축 해제 결과는 같음 |
| `DIFFERENT_DECODED_BYTES_WITH_SAME_SIZE_AND_CRC` | 해제한 바이트도 다르지만 길이와 CRC가 같음 |
| `REFERENCE_TARGET_ABSENT` | 정답 대상 블록이 표본에 없어 비교하지 못함 |
| `REFERENCE_SIGNATURE_MISMATCH` | 정답 위치에 예상한 다음 헤더 시그니처가 없어 추가 검토 필요 |
| `REFERENCE_PAIR_NOT_PASS` | 실제 정답 대상을 연결해도 PASS하지 않아 추가 검토 필요 |

바이트가 같다는 결과는 비교한 구간의 내용 일치를 뜻하며, 원본 아카이브 전체의 동일성이나
어떤 파일이 다른 파일을 복사했다는 이력을 증명하지 않는다. 정답 대상 재구성은 사후 진단
전용이다. 이 보고서는 점수·후보·grouping 입력을 변경하지 않으며 기존 출력도 덮어쓰지 않는다.
