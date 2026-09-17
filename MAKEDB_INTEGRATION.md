# MakeDB 기반 DEF/LEF reader

`XP_shared_memory/bin_sep/dreamplace`의 MakeDB, ReadDEF, ReadLEF,
LEF_DEF_analysis, Cell, Net, Pin, Die, NameIdMap 및 관련 Python 모듈을
XP_timing_4.1에 복사하여 연결했다. **bin_sep 원본은 수정하지 않는다.**

## 실행 경로

`PlaceDB.read → MakeDBAdapter → MakeDB.readDB → ReadDEF/ReadLEF → Analysis`

DEF 입력에서는 기존 C++ PlaceIO reader를 호출하지 않는다. MakeDB의
node/pin/net 이름과 ID, CSR 연결 배열을 PlaceDB에 직접 전달한다.
Bookshelf 입력은 기존 C++ reader를 유지한다.

노드 순서는 참고 MakeDB와 동일하게 movable standard cell, movable macro,
fixed standard cell, fixed macro, blockage, external pin 순이다. 각 그룹 내부의
정렬도 MakeDB가 수행한다. net/pin ID는 MakeDB가 생성한 순서를 보존한다.
원래 C++ reader로 만든 ID 파일은 재사용하면 안 된다.

참고 Analysis의 SIGNAL net/port 필터와 연결된 셀 선택 규칙을 유지한다.
따라서 DEF의 모든 COMPONENT가 반드시 placement node가 되는 것은 아니다.
원본 프로젝트의 custom objective, shared-memory master/client, square-stretch
후처리까지 이식한 것은 아니다. 현재 DREAMPlace의 배치 엔진을 사용한다.

## 설정

기존 DREAMPlace 배치 설정에 다음 항목을 추가한다.

```json
{
  "db_option": "def",
  "mode": "binary_write",
  "def_path": "/path/design.def",
  "lef_dir_path": ["/path/technology.lef", "/path/cell_lefs"],
  "save_path": "/path/new_saved_db"
}
```

`def_input`, `lef_input`도 지원한다. `def_path`, `lef_dir_path`가 비어 있지
않으면 이들이 우선한다. LEF 파일/디렉터리 목록 전체를 읽는다.
동일 master의 서로 다른 물리 모델을 한 목록에 섞지 않는다.

```bash
cd /mnt/hdd1/XP_timing_4.1/bin
python3 dreamplace/Placer.py /path/config.json
```

| db_option | mode | physical DB 동작 |
|---|---|---|
| def | binary_write 또는 read/binary_write | DEF/LEF 파싱 후 명시적으로 저장 |
| def | 그 외 | 파싱만 수행; 영구 DB를 생성하지 않음 |
| binary | 무관 | 저장된 geometry/connectivity/초기 위치 복원 |
| binary_wo_pos | 무관 | 저장된 geometry/connectivity 복원, movable x/y만 0으로 초기화 |

binary 모드에서 `mode=binary_write`가 남아 있어도 읽기만 한다.
두 복원 모드 모두 fixed cell과 IO 위치를 유지한다. `read_posX/Y/orient`가
있으면 복원된 초기 상태 위에 적용한다. DEF/LEF로 자동 fallback하지 않는다.

## 새 binary 저장 형식

`save_path/physical_db`에 다음 내용을 저장한다.

- node/pin/net 이름, 위치, 방향, 크기, pin offset, net weight: `.npy`
- pin2node/pin2net 및 node2pin/net2pin의 flat/start CSR 배열: `.npy`
- row, fence region, node2fence 정보: `.npy`
- 개수, 단위, die 범위, row 방향, placement-only net 목록: `manifest.json`
- 원본 DEF 전체 내용: `template.def` — 최종 DEF 작성 시 COMPONENT 위치만 수정

숫자/문자열 NPY에는 pickle을 쓰지 않는다. 가변 길이 object 배열 대신 CSR을
저장한다. 복원 때 대규모 `cells_info.json`, `lef_info.json`을 다시 읽지 않는다.
DEF 템플릿도 결과 DEF를 작성할 때만 읽으며 ID 복원에는 사용하지 않는다.
참고 parser가 생성하는 부가 JSON/TXT는 명시적 export에서만 save_path에 남긴다.
일반 DEF 읽기에서는 Analysis의 주요 JSON/TSV/NPY 및 MakeDB의 node/pin
중간 파일 쓰기를 건너뛴다. pin별 디버그 dictionary도 만들지 않는다.

좌표와 canonical N pin offset/LEF 크기는 **scale 이전 DEF 단위**로 저장한다.
orientation은 로드 후 한 번만 적용한다. filler는 다시 생성한다.
동적 RC, slack, criticality는 저장하지 않는다. 저장된 net weight는 초기 weight다.

기존 physical snapshot을 덮어쓰지 않는다. 다시 export하려면 새 save_path를
지정한다. 불완전하거나 손상된 snapshot은 오류로 처리한다.

## 기존 bin_sep 저장물 읽기

`physical_db`가 없으면 기존 `num_node_info.npy`, `node_names.txt`,
각종 TXT/NPY mapping, `cells_info.json`, `ext_pin_info.json`을 직접 읽는다.
원본 DEF/LEF reader를 호출하거나 저장물을 갱신하지 않는다.

- `num_node_info.npy`는 셀 종류별 개수를 포함한 12항목 형식이어야 한다.
- `pinInfo_dict.json`이 필요하다. 이 파일의 변환 전 pin offset을 사용하여
  기존 저장물의 orientation 중복 변환을 방지한다.
- 기존 object NPY는 pickle을 사용하므로 **신뢰하는 본인 저장물만** 읽는다.
- legacy `binary`에는 `read_posX`, `read_posY`, `read_orient`를 지정한다.
  위치 없이 읽으려면 `binary_wo_pos`를 사용한다.
- `read_node_names`를 지정하면 저장된 ID/이름 순서와 일치하는지 검사한다.
- MakeDB 경로는 기존 한 열짜리 위치 파일과 새 `cell_id value` 파일을 모두
  읽는다. 한 파일 안에서 두 형식을 섞을 수 없다. ID는 physical node 기준이며
  filler나 metrics footer 행을 포함하면 안 된다. 출력은 `cell_id value` 형식이다.

legacy 저장물에는 원본 DEF의 모든 section이 없을 수 있다. 이 경우 기본 결과는
`.pl`이며 `write_posX/Y/orient`도 사용할 수 있다. 완전한 DEF 출력이 필요하면
`def_template_input`에 해당 디자인의 원본 DEF를 지정한다. 이 파일은 ID 생성이나
binary 복원에 쓰지 않고 결과 DEF 작성용으로만 사용한다. 새 export는 템플릿을
함께 저장하므로 원본 DEF 없이도 DEF 출력이 가능하다.

## 현재 배치·timing 인터페이스와의 차이 처리

- 초기 DEF의 위치/방향을 읽는다. 참고 MakeDB처럼 standard cell 좌표를 -1로
  버리지 않는다. 기존 `random_center_init_flag` 설정은 여전히 적용된다.
- standard-cell pin은 참고 MakeDB와 동일한 셀 중심 근사를 유지한다.
  macro/fixed cell은 참고 LEF pin geometry를 사용한다.
- 원본의 net_weight_deltas 중복 append 및 criticality_deltas 누락을 수정했다.
- orientation 변환은 PlacementState의 8방향 bbox 변환을 사용한다. ID는 바뀌지 않는다.
- physical pin 이름 `instance pin`, `PIN port`는 그대로 보존한다. 별도의 timing
  이름 view만 `instance:pin`, `port`로 만든다. ID는 동일하다.
- RC root는 flat pin 배열의 첫 항목이 아니라 OpenTimer의 실제 driver로 찾는다.
- parser가 추가한 placement-only net은 STA/RC와 timing weight 갱신에서 제외한다.
- timer에만 존재하는 net은 weight 배열에 접근하지 않는다. 실제 physical net/pin의
  timing 연결 불일치는 Python 오류로 보고한다.

LIB/Verilog/SDC 모델은 [TIMING_CACHE.md](TIMING_CACHE.md)의 같은 명시적 저장 계약을
따른다. legacy physical DB만으로 timing 모델을 만들어낼 수는 없다. timing을
사용할 binary DB에는 대응하는 `timing_cache`도 준비해야 한다. 이번 native timing
backend 변경 이전에 만든 timing cache는 새 backend로 다시 export해야 한다.

## DEF reader 성능 개선 (2026-09-17)

- 외부 pin마다 NameIdMap.keys() 전체 목록을 만들지 않고 직접 조회한다.
- Net.add_cell_pin은 LEF pin dictionary를 사용한다.
- Pin.add_net은 큰 fanout(32개 이상)에만 중복 검사 set을 추가한다.
  공개 netList의 순서는 유지한다. 작은 연결에는 set을 만들지 않는다.
- DEF의 원본 줄과 strip한 줄을 동시에 보관하지 않는다.
- node 분류 시 반복 pandas .loc 조회, net weight 적용 시 반복 list.index를 제거한다.
- 일반 읽기의 불필요한 metadata 생성/쓰기를 생략한다. 명시적 binary_write의
  JSON/TXT sidecar와 physical binary 저장은 그대로 수행한다.

현재 스레드는 여전히 **record 추출만** 병렬화한다. 실제 객체 생성과 연결은
직렬이며 로그에서 `extract`와 `serial parse/build`를 분리하여 표시한다.
`def_parse_num_threads`가 있으면 그 값을, 없으면 `num_threads`를 사용한다.
LEF 읽기, DEF 읽기, Analysis, node 배열, pin/net 배열, CSR/region, metadata 저장
시간도 각각 로그에 나온다. 스레드 수 증가가 항상 가속을 의미하지는 않는다.

스레드 결과를 완료 순서가 아니라 DEF 입력 순서로 합치도록 수정했다.
이제 1/2/4/8 스레드에서 동일한 ID/연결 결과를 얻는다. **이전 버전에서 여러
스레드로 새로 파싱하여 생성한 net/pin ID는 실행 순서에 따라 달랐을 수 있다.**
기존 binary DB의 ID는 변경하지 않지만, 새로 파싱한 DB에 과거 net/pin ID 파일을
혼용하지 않는다. node 그룹별 정렬 규칙은 변경하지 않았다.

성능 비교는 임시 디렉터리의 합성 입력으로 재현할 수 있다.

```bash
OMP_NUM_THREADS=1 python3 benchmark_makedb.py --cells 20000 --ports 1000 --threads 1
OMP_NUM_THREADS=1 python3 benchmark_makedb.py --cells 20000 --ports 1000 --threads 4
OMP_NUM_THREADS=1 python3 benchmark_makedb.py --cells 20000 --ports 1000 --threads 1 --export
```

`--reader-dir`로 비교할 다른 설치본의 dreamplace 디렉터리를 지정할 수 있다.
결과에는 소요 시간, 배열 체크섬, 단계별 로그가 포함된다. 측정 구간은 adapter.read
및 선택적 save이며 lazy Python 모듈 import도 포함하고, STA/배치는 포함하지 않는다.
동일 환경에서 단일 스레드로 1회씩 측정한 예:

| 모드 | 수정 전 | 수정 후 |
|---|---:|---:|
| 일반 DEF 읽기 | 6.89초 | 3.18초 |
| 명시적 export 포함 | 6.85초 | 4.67초 |

네 실행의 배열 체크섬은 일치했다. 측정값은 합성 데이터의 예이며 실제 디자인의
속도 향상이나 스레드 확장성을 보장하지 않는다. native timing 코드는 이 성능
수정에서 변경하지 않았다.

## 검증

```bash
OMP_NUM_THREADS=1 \
DREAMPLACE_INSTALL=/mnt/hdd1/XP_timing_4.1/bin \
DREAMPLACE_TIMING_CPP=/mnt/hdd1/XP_timing_4.1/bin/dreamplace/ops/timing/timing_cpp.cpython-310-x86_64-linux-gnu.so \
python3 -m unittest test_makedb_adapter test_timing_cache test_placement_state -q
```

ID 보존, 여러 LEF 입력, fixed cell과 macro 방향, orientation override,
원본 없는 binary 복원, legacy 읽기 전용 동작, legacy 위치 파일, DEF/좌표 출력,
driver가 첫 pin이 아닌 RC 계산 및 기존 timing 모델 복원을 검사한다.
실제 대규모 사용자 디자인에 대한 성능/메모리 측정은 아직 하지 않았다.
