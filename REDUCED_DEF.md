# ReducedDEF: 클러스터 DEF 생성

`ReducedDEF.py`는 원본 DEF의 물리적 floorplan을 보존하면서,
`ReducedVerilog.py`로 생성한 reduced Verilog와 일치하는 배치용 DEF를 만듭니다.
원본 파일은 변경하지 않으며, 이미 존재하는 출력 파일은 덮어쓰지 않습니다.

## 실행

```bash
cd /mnt/hdd1/XP_timing_4.1/bin
python3 dreamplace/ReducedDEF.py \
  --input /path/original.def \
  --verilog /path/reduced.v \
  --mapping /path/cluster_mapping.jsonl \
  --cluster-lef /path/cluster_cells.lef \
  --output /path/reduced.def
```

입력은 동일한 클러스터 결과에서 만들어야 합니다.

| 옵션 | 입력 |
| --- | --- |
| `--input` 또는 `--def` | 원본 DEF |
| `--verilog` | ReducedVerilog.py 출력, flat non-ANSI structural Verilog |
| `--mapping` | ReducedLiberty.py의 cluster_mapping.jsonl |
| `--cluster-lef` 또는 `--lef-dir` | 클러스터 및 원본 셀 LEF가 있는 디렉터리(하위 폴더 포함), 또는 기존 단일 클러스터 LEF |
| `--output` | 새 DEF; `출력경로.summary.json`도 생성 |
| `--placement` | `centroid` 기본값, 또는 `unplaced` |
| `--positions` | 선택 사항: 모든 cluster_id의 x/y/orient 지정 |
| `--temp-dir` | 대규모 연결 정보를 임시 저장할 디렉토리 |
| `--drop-specialnets` | SPECIALNETS를 명시적으로 제거 |
| `--drop-groups` | GROUPS 제약을 명시적으로 제거 |

Verilog만으로는 제거해야 하는 원본 셀 목록과 새 셀의 물리적 크기를 알 수
없으므로 mapping과 cluster LEF도 필요합니다. mapping 옆에 manifest.json이
있으면 status가 complete여야 합니다.

## 여러 LEF를 디렉터리에서 읽기

클러스터 LEF와 남아 있는 원본 셀 LEF가 함께 있는 디렉터리를 지정할 수 있습니다.

```bash
python3 dreamplace/ReducedDEF.py \
  --input /path/original.def \
  --verilog /path/reduced.v \
  --mapping /path/cluster_mapping.jsonl \
  --lef-dir /path/all_lefs \
  --output /path/reduced.def
```

`--cluster-lef /path/all_lefs`도 같은 동작입니다. 디렉터리의 모든 하위 폴더에서
`.lef` 파일을 찾으며 확장자의 대소문자를 구분하지 않습니다. 파일 경로를 정렬해
읽고, 같은 실제 파일을 가리키는 파일 symlink는 한 번만 읽습니다. 디렉터리
symlink 내부는 별도로 순회하지 않으므로 실제 디렉터리 아래에 배치하세요.
기술 LEF도 폴더에 함께 둘 수 있지만 기술 LEF가 반드시 필요한 것은 아닙니다.

디렉터리 모드에서는 다음을 검증합니다.

- 새 클러스터의 master, 정확한 pin 집합/방향, width/height.
- DEF에 유지되는 원본 component의 master 존재 여부. Verilog에 없는 filler 등도 포함합니다.
- reduced Verilog에 유지되는 원본 셀 연결 pin이 해당 LEF master에 존재하는지 여부.
- 동일한 master가 두 번 정의되면 경로를 표시하고 중단합니다. 여러 library variant를
  모아 두어 중복된 경우 사용할 한 종류만 별도 디렉터리에 모아 지정하세요.

원본 셀의 위치·방향·크기를 이 기능이 변경하지는 않습니다. 원본 셀 크기의 geometry
검증, 원본 pin의 논리 방향 일치 검증 또는 routing/DRC 검증을 수행하지 않습니다.
summary JSON의 `lef_files`에 읽은 파일 목록, `lef_master_sources`에 master별
파일 경로, `retained_lef_validated`에 원본 master/pin 존재 검사 여부를 기록합니다.

기존 `--cluster-lef /path/cluster_cells.lef` **단일 파일 방식은 호환 유지**합니다.
이 경우 클러스터만 검사하며 원본 셀 LEF를 요구하지 않습니다.
`retained_lef_validated=false`로 기록됩니다. 원본 셀도 검사하려면 디렉터리를 사용하세요.

이 옵션은 ReducedDEF 변환기에만 적용됩니다. 이후 DREAMPlace를 실행할 때도
원본 셀과 클러스터 LEF가 모두 입력되도록 배치 설정을 별도로 지정해야 합니다.

## 변경 및 보존 내용

- COMPONENTS: mapping에 포함된 원본 셀을 제거하고 각 클러스터 인스턴스를 추가합니다.
  새 인스턴스 이름은 reduced Verilog에서 읽습니다. 이름 충돌 회피용 접미사도 일치합니다.
  남아 있는 원본 component 레코드는 위치, 방향, 속성 등을 그대로 보존합니다.
- NETS: reduced Verilog의 모든 인스턴스 핀과 top-level port 연결로 재구성합니다.
  완전히 내부화된 net은 사라집니다. 연결 없는 wire 선언은 DEF net으로 만들지 않습니다.
  원본에 NETS 섹션이 없어도 새로 생성합니다.
- DIEAREA, ROW, TRACKS, GCELLGRID, UNITS, PINS 등은 원본 텍스트를 보존합니다.
  유지되는 일반 net의 USE도 보존합니다. 새 net의 기본 USE는 SIGNAL입니다.
- 원본 일반 NETS의 ROUTED, WEIGHT, NDR 등 **USE 이외의 속성은 제거**합니다.
  이 출력은 routing 결과 보존용이 아닌 초기 배치용 DEF입니다.
- 논리 Verilog에 없는 filler 등의 물리 전용 component도 보존합니다.
  단, 이들이 원본 일반 NETS의 signal endpoint이면 자동으로 연결을 버리지 않고 오류를 냅니다.

기존 component/node ID를 보존하는 변환은 아닙니다. 새 DEF를 MakeDB로 읽으면
클러스터 포함 새 node_names/ID가 생성되므로, 원본 ID 기반 위치 파일이나 binary
캐시를 그대로 사용하지 마세요. 새로운 save_path로 필요한 캐시를 생성하세요.

## 초기 위치와 단위

기본값 `--placement centroid`에서는 멤버들의 **DEF 배치 기준점 x/y의 산술평균**을
새 클러스터의 중심으로 삼고 LEF 크기의 절반을 빼서 좌하단을 추정합니다.
면적 가중 중심이나 원래 셀들의 실제 중심 평균은 아닙니다. 방향은 N입니다.
좌표는 정수 DBU로 반올림하고 die의 bounding box 내부로 제한합니다.
멤버 하나라도 UNPLACED이면 그 클러스터도 UNPLACED로 생성합니다.
`--placement unplaced`는 모든 새 클러스터를 UNPLACED로 생성합니다.

LEF SIZE는 **µm**이며 DEF의 UNITS DISTANCE MICRONS를 곱해 DBU로 변환합니다.
별도 위치를 지정할 때는 다음과 같이 **정수 DEF DBU**를 입력합니다.

```text
cluster_id	x	y	orient
7	20000	10000	N
8	50000	10000	FN
```

```bash
python3 dreamplace/ReducedDEF.py ... --positions cluster_positions.tsv
```

탭/쉼표/공백 표를 읽으며 모든 클러스터를 한 번씩 지정해야 합니다.
이 좌표는 `--placement`보다 우선합니다. die bounding box 밖의 명시적 좌표는
자동 수정하지 않고 오류를 냅니다. 회전 E/W/FE/FW의 크기 교환도 검사합니다.
초기 위치는 legalization 결과가 아닙니다. 겹침, row/site/grid 정렬, polygon die와
region 내부 포함 여부 및 LEF symmetry에 따른 방향 적법성은 검증하지 않습니다.

## 안전 검사와 제한

- 원본 DEF와 reduced Verilog의 DESIGN/module 이름 및 top port가 일치해야 합니다.
  DEF signal pin의 NET 이름은 같은 이름의 Verilog port와 일치해야 합니다.
- 클러스터 master/pin/연결은 mapping 및 LEF와 일치해야 합니다.
  멤버 누락, 중복, 기존 component와 새 인스턴스 이름 충돌을 거부합니다.
- FIXED/COVER 멤버는 클러스터로 교체할 수 없습니다.
- 서로 다른 named REGION에 속한 셀들을 합치지 않습니다. 같은 REGION은 상속합니다.
  inline REGION box 구문은 지원하지 않습니다.
- SPECIALNETS가 제거될 셀 또는 wildcard를 참조하면 오류를 냅니다.
  PG 재연결은 수행하지 않습니다. 명시적 `--drop-specialnets`는 배치 전용일 때만
  사용하세요. 이 옵션으로 원본 POWER/GROUND PINS까지 고립되면 역시 거부합니다.
- 비어 있지 않은 GROUPS는 자동 재매핑하지 않습니다. `--drop-groups`로 제거하면
  원본 grouping 제약이 사라지므로 의도한 경우에만 사용하세요.
- 다른 counted section에서 제거되는 셀/net 참조가 발견되면 오류를 냅니다.
  이 검사는 보수적인 토큰 비교이므로 특수 속성 문자열에도 걸릴 수 있습니다.
- 지원 범위는 ReducedVerilog와 동일한 flat named-pin 연결입니다. 계층 module,
  assign, inout, 상수 연결을 추가 elaboration하지 않습니다.
- DEF counted section의 헤더 및 END는 각각 독립된 줄이어야 합니다. record는
  여러 줄 또는 한 줄의 여러 record를 지원합니다. DESIGN/UNITS/DIEAREA도 독립된
  줄이어야 합니다. 모든 DEF 확장을 지원하는 범용 parser는 아닙니다.

대규모 NETS는 임시 SQLite 파일에 저장하고 DEF는 streaming으로 읽습니다.
따라서 연결 전체를 Python 객체로 유지하는 비용을 줄이지만 디스크 공간과 I/O가
필요합니다. SSD 임시 경로는 `--temp-dir`로 지정할 수 있습니다.

## DREAMPlace / OpenTimer 연결

배치 입력에는 **원본 technology LEF + 남은 셀들의 원본 LEF + cluster LEF + reduced DEF**를
사용합니다. timing 입력에는 **남은 셀들의 원본 Liberty + reduced Liberty + reduced Verilog**가
필요합니다. DEF와 Verilog의 클러스터 인스턴스 이름이 같아야 위치에서 계산한 RC를
timing net에 연결할 수 있습니다.

SDC/SPEF를 이 스크립트가 재작성하지는 않습니다. SDC에서 제거된 내부 cell/pin을
참조하는 예외 제약은 별도로 재매핑해야 하며 원본 SPEF를 그대로 사용할 수 없습니다.
중앙 pin 기반 coarse RC와 reduced Liberty의 내부 delay 근사는 별도의 모델 오차입니다.

## 검증

```bash
cd /mnt/hdd1/XP_timing_4.1/dreamplace
DREAMPLACE_INSTALL=/mnt/hdd1/XP_timing_4.1/bin OMP_NUM_THREADS=1 \
  python3 -m unittest test_reduced_def -q
```

단위 테스트에 MakeDB 재입력과 DREAMPlace native DEF reader 재입력이 포함됩니다.
전체 대규모 벤치마크의 배치 또는 STA 완료를 의미하지는 않습니다.
