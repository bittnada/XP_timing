# 배치 전용 cluster LEF / DEF 생성

`ClusterPlacement.py`는 Leiden membership에서 **Reduced Liberty/Verilog 없이**
전체 replacement LEF와 축약 DEF를 생성한다. 원본 OpenTimer와의 연동, timing DB 생성, GPU 배치는
이번 생성기의 범위가 아니다. 기존 `ReducedLEF.py`, `ReducedDEF.py`,
`timing_flow.sh`의 timing-characterized 흐름도 변경하지 않는다.

## 실행

`XP_timing_4.1/bin`에서:

```sh
TC_UTILIZATION=0.8 sh run_cluster_placement.sh
```

기본 출력은 `results/superblue1/timing_flow_v2/PLACEMENT_U0.8`이다.
utilization은 사용자가 반드시 지정한다. 기존 디렉토리는 덮어쓰지 않는다.
출력 경로는 `TC_PHYSICAL`, 실행 데이터는 `TC_RUN`, `TC_BENCH`, `TC_SAVE`,
pin layer는 `TC_PIN_LAYER`로 지정할 수 있다.

직접 실행하는 예:

```sh
python3 dreamplace/ClusterPlacement.py \
  --clusters results/superblue1/timing_flow_v2/leiden/clusters.tsv \
  --cell-names results/superblue1/timing_flow_v2/cell_names.tsv \
  --saved-db results/superblue1/timing_edges_protected/save \
  --def-input benchmarks/iccad2015.ot/superblue1/superblue1_withnets.def \
  --lef-input benchmarks/iccad2015.ot/superblue1/superblue1.lef \
  --utilization 0.8 \
  --pin-layer metal2 \
  --output results/superblue1/timing_flow_v2/PLACEMENT_U0.8
```

입력 DEF에는 COMPONENTS와 NETS가 있어야 한다. 실제 연결은 **원본 DEF**를
기준으로 읽는다. `--saved-db`에서는 `lef_info.json`의 master별 width/height,
class, pin direction과 `die_info.json`을 사용한다. 원본과 일치하는 saved DB를
지정해야 한다. membership ID는 `--cell-names`의 ID이며 DEF 나열 순서가 아니다.
TSV/CSV/공백 형식의 기존 membership 로더를 재사용한다.

## 전체 utilization과 자동 크기 결정

`--utilization`은 cluster 내부 utilization이 아니라 fixed cell까지 포함한 설계 전체
목표 utilization이다. saved physical DB의 placeable area는 fixed 점유를 제외하므로
fixed 면적을 다시 더해 fixed 점유 전 placeable area를 복원한다.

```text
current_utilization = (fixed_area + movable_area) / placeable_area

target <= current: movable_area_inflation = 1
target > current:  movable_area_inflation =
                   (target × placeable_area - fixed_area) / movable_area
```

fixed cell은 계산에는 포함하지만 절대 키우지 않는다. 동일한 면적 배율을 cluster와
비cluster movable cell에 적용한다. 비cluster cell은 원래 aspect ratio를 유지하면서,
cluster는 die aspect ratio에 가깝게 폭/높이를 정한다. 두 경우 모두 폭은 site width,
높이는 row height 배수로 올리고 목표 면적보다 작아지지 않게 한다. 격자 올림 때문에
최종 achieved utilization은 요청값보다 조금 높을 수 있다.

`die_info.json`의 `site_width`, `row_height`는 **DEF DBU**이며
`def_scale`로 나누면 µm이다. superblue1의 값은:

```text
site_width = 380 DBU → 0.19 µm
row_height = 3420 DBU → 1.71 µm
def_scale  = 2000 DBU/µm
```

fixed와 movable instance가 같은 원본 master를 공유하면 원본 master는 fixed용으로
보존하고 `INF_<master>`를 생성해 movable instance만 새 master로 DEF에서 바꾼다.
inflated master의 pin/OBS 좌표도 새 폭과 높이에 맞춰 축별로 scaling한다.

## pin / net 규칙

- 각 `(cluster, original_net)`마다 서로 다른 `VP0`, `VP1`, ...를 생성한다.
- pin 번호는 원본 DEF의 net 등장 순서를 따른다. 동일 입력에 결정적이다.
- 동일 net의 같은 cluster 소속 원본 pin들은 하나로 줄인다.
- 서로 다른 net은 endpoint가 같더라도 절대로 병합하지 않는다.
- 모든 cluster pin rectangle은 cluster 정중앙이다.
- 원본 셀은 `PC_<cluster_id>` master와 `__pc_cluster_<cluster_id>` instance로 교체한다.
- cluster LEF는 movable multi-row 배치 셀을 위한 `CLASS CORE`, 원본 ROW SITE를 사용한다.
- 미병합 component 레코드와 외부 PINS, ROW, DIEAREA 등은 보존한다.
- 원본 NETS routing/property 등 USE 이외 속성은 제거한다.
- net 내부에 cluster의 OUTPUT이 있으면 VP는 OUTPUT, 없으면 INPUT이다.
  원본 INOUT이 포함되면 INOUT을 사용한다. 이는 배치용 방향 표시이며 논리 모델이 아니다.

**내부 net도 DEF에는 남긴다.** cluster 내부에서만 끝나는 net은 VP 하나짜리 net이 된다.
현재 MakeDB의 Analysis는 degree <= 1 및 non-SIGNAL net을 배치 배열에서 제외한다.
따라서 DEF net 수와 로딩 후 PlaceDB net 수는 다를 수 있다. 생성기는 해당
기존 동작을 바꾸지 않는다. 원본 timing DB는 이 net들을 보존해야 하며, 이후
숫자 ID mapping에서는 배치에 없는 net을 명시적으로 처리해야 한다.

## 출력

| 파일 | 내용 |
|---|---|
| `placement.lef` | 원본/fixed master, inflated movable master, cluster master를 모두 포함한 complete LEF |
| `reduced.def` | 축약 COMPONENTS, 원본 net 정체성을 유지한 NETS |
| `cluster_sizes.tsv` | 면적 합, µm/DBU 크기, site/row 수, 공통 inflation, pin 수 |
| `inflated_masters.tsv` | movable master의 원래/변경 크기와 실제 면적 배율 |
| `cell_mapping.tsv` | 모든 원본 component → 축약 component/master; member ID는 cluster 멤버만 기록 |
| `pin_mapping.tsv` | 원본 DEF endpoint → 축약 endpoint; net 이름 포함 |
| `net_mapping.tsv` | 원본/축약 net 이름, 축약 전후 degree, degree >= 2 여부 |
| `manifest.json` | 완료 표시, 입력 경로/크기/mtime, 통계 및 제한사항 |

mapping은 **이름 기반**이다. 향후 두 DB를 읽은 뒤 ID를 확정하는 데 사용할 수
있지만, 아직 최종 PlaceDB/TimingDB 숫자 ID 배열이나 fingerprint 검증기는 아니다.
두 물리 DB를 저장한 뒤 `PlacementTimingMapping.py`로 숫자 ID 배열을 생성할 수
있다. 사용법과 검증 조건은 `PLACEMENT_TIMING_MAPPING.md`를 참고한다.
`wirelength_nontrivial`은 단순히 degree >= 2이며 USE CLOCK 등의 reader 필터를
모두 반영한 활성화 플래그가 아니다.

`placement.lef`는 complete replacement LEF이므로 원본 LEF나 이전 cluster LEF와
동시에 읽지 않는다.

## 초기 위치와 제한사항

기본 `--placement centroid`는 구성 셀의 면적 가중 중심을 기준으로 cluster를
놓고 core bbox 내의 site/row grid에 맞춘다. 멤버가 하나라도 UNPLACED이면
해당 cluster도 UNPLACED이다. `--placement unplaced`로 모두 미배치 상태로
출력할 수도 있다. 원본 셀 방향이 E/W 계열이면 중심 계산에 회전된 폭/높이를 사용한다.

이는 **초기 위치이지 legalization 결과가 아니다**. overlap과 blockage는
해결하지 않는다. nonrectangular DIEAREA, 혼합 ROW SITE, 세로/다중 ROW 배열,
cluster 멤버의 REGION 제약, FIXED/COVER 또는 non-CORE 멤버,
component-associated BLOCKAGES, nonempty SPECIALNETS/GROUPS/SCANCHAINS 등은
안전하게 중단한다. 일반 rectangular placement BLOCKAGES는 그대로 보존한다.
기존 TimingCluster/Leiden의 eligibility/clock/sequential 조건을 재검사하지 않는다.

`--pin-width`의 기본 0.1 µm는 **placeholder이지 공정 규칙이 아니다**.
`--pin-height` 기본값은 pin-width와 같다. `--pin-layer`는 필수이며 실제 원본
LEF의 layer를 사용해야 한다. 이 버전은 tech LEF를 요구하거나 DRC 검증하지 않는다.
중앙 pin들은 겹치므로 실제 routing용이 아니며 OpenTimer에 축약 회로로 넣지 않는다.

## 테스트

```sh
python3 -B -m unittest test_cluster_placement -v
```

자동 크기/격자/단위, 원본 보존, 내부 net 보존, 동일 cluster 쌍의 여러 net 유지,
미병합 셀 유지, 중복/누락 입력 거부, 기존 결과 보호, ReadDEF/MakeDB round-trip을 검증한다.
