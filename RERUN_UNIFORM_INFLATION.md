# Uniform-inflation cluster placement 재실행 가이드

이 문서는 fixed cell을 제외한 모든 movable cell과 cluster cell에 동일한 면적
inflation을 적용하여 complete placement LEF, reduced physical DB, ID mapping,
two-DB placement 및 비교 결과를 다시 생성하는 전체 절차를 정리한다.

## 1. 적용된 utilization 및 inflation 정책

설계의 현재 utilization은 fixed cell을 포함하여 계산한다.

```text
current_utilization = (fixed_area + movable_area) / placeable_area
```

fixed cell은 위 계산에는 포함하지만 크기를 변경하지 않는다. 사용자가 지정한
`target_utilization`이 현재 utilization보다 클 때만 movable cell을 확대한다.

```text
target_total_area = target_utilization * placeable_area

movable_area_inflation =
    (target_total_area - fixed_area) / movable_area
```

`target_utilization <= current_utilization`이면 다음과 같이 추가 inflation을 하지 않는다.

```text
movable_area_inflation = 1
```

동일한 면적 배율을 다음 두 대상에 적용한다.

- cluster로 합쳐지지 않은 movable cell
- cluster를 구성하는 원본 cell들의 합산 면적

fixed cell은 원래 크기를 유지한다. 동일한 원본 LEF master를 fixed와 movable
instance가 함께 사용하면 원본 master는 fixed용으로 보존하고, movable용
`INF_<original_master>`를 별도로 생성한다.

비cluster movable cell은 기존 aspect ratio에 가깝게 크기를 정한다. Cluster cell은
die aspect ratio에 가깝게 크기를 정한다. 폭은 site width, 높이는 row height의
정수배로 올리며 목표 면적보다 작아지지 않게 한다. 따라서 격자 올림 때문에 실제
달성 utilization은 요청값보다 조금 높을 수 있다.

Inflated movable master의 pin/OBS 좌표도 새로운 폭과 높이에 맞춰 축별로 scaling한다.

## 2. 생성 파일 정책

`ClusterPlacement.py`는 다음 파일을 한 번에 생성한다.

| 파일 | 내용 |
|---|---|
| `placement.lef` | 원본/fixed master, inflated movable master, cluster master를 모두 포함한 complete LEF |
| `reduced.def` | inflated master 및 cluster instance를 사용하는 reduced DEF |
| `cluster_sizes.tsv` | cluster별 원본 면적, 최종 크기, 공통 inflation 및 실제 member utilization |
| `inflated_masters.tsv` | movable master별 원래/변경 크기와 실제 면적 배율 |
| `cell_mapping.tsv` | 원본 instance에서 reduced instance/master로의 대응 |
| `pin_mapping.tsv` | 원본 pin에서 reduced pin으로의 대응 |
| `net_mapping.tsv` | 원본/reduced net 대응 |
| `manifest.json` | utilization, 면적, 입력 fingerprint 및 생성 통계 |

`placement.lef`는 complete replacement LEF이다. Reduced DB를 만들 때 원본
`superblue1.lef` 또는 과거의 `cluster_cells.lef`를 같이 읽으면 안 된다.

## 3. 실행 전 주의사항

- 모든 명령은 `/mnt/hdd1/XP_timing_4.1/bin`에서 실행한다.
- 각 생성기는 기존 출력 디렉터리를 덮어쓰지 않는다.
- 기존 `PLACEMENT_U0.7` 결과는 그대로 보존한다.
- superblue1에서 측정된 현재 utilization은 약 `0.768886`이다.
- 따라서 목표 `0.7`을 지정하면 inflation이 발생하지 않는다.
- 아래 명령은 목표 utilization `0.9`을 사용하는 예다.
- 다른 목표를 사용할 때는 `TARGET`, `PHYSICAL`, `CONFIG`, `RESULT` 이름을 함께 바꾼다.

## 4. 전체 재실행 순서

### 4.1 공통 변수 설정

```bash
cd /mnt/hdd1/XP_timing_4.1/bin

export TARGET=0.7
export PHYSICAL=results/superblue1/timing_flow_v2/PLACEMENT_U0.7
export CONFIG=dreamplace/examples/two_db_superblue1_u07.json
export RESULT=results/two_db_timing_u07
```

### 4.2 Complete placement LEF와 reduced DEF 생성

```bash
TC_UTILIZATION="$TARGET" \
TC_PHYSICAL="$PHYSICAL" \
sh ./run_cluster_placement.sh
```

생성 여부를 확인한다.

```bash
ls -lh \
  "$PHYSICAL/placement.lef" \
  "$PHYSICAL/reduced.def" \
  "$PHYSICAL/cluster_sizes.tsv" \
  "$PHYSICAL/inflated_masters.tsv" \
  "$PHYSICAL/manifest.json"
```

계산된 utilization과 inflation을 확인한다.

```bash
python3 -c "import json; d=json.load(open('$PHYSICAL/manifest.json')); print('current =', d['current_utilization']); print('target =', d['target_utilization']); print('inflation =', d['movable_area_inflation']); print('achieved =', d['achieved_utilization']); print('fixed area =', d['original_fixed_area_um2']); print('original movable area =', d['original_movable_area_um2']); print('reduced movable area =', d['reduced_movable_area_um2'])"
```

superblue1에서 목표 `0.7`을 사용한 검증 결과는 다음과 같다.

```text
current_utilization    = 0.7688856061
target_utilization     = 0.9
movable_area_inflation = 1.1361283120
achieved_utilization   = 0.9031253596
```

### 4.3 Reduced physical DB 생성

```bash
TC_PHYSICAL="$PHYSICAL" \
sh ./run_prepare_placement_db.sh
```

완료 파일을 확인한다.

```bash
test -f "$PHYSICAL/save/physical_db/manifest.json" && echo "physical DB complete"
```

이 단계는 다음 입력만 사용한다.

```text
$PHYSICAL/reduced.def
$PHYSICAL/placement.lef
```

### 4.4 Original timing DB와 reduced placement DB 사이 ID mapping 생성

```bash
TC_PHYSICAL="$PHYSICAL" \
sh ./run_placement_timing_mapping.sh
```

완료 파일을 확인한다.

```bash
test -f "$PHYSICAL/ID_MAPPING/manifest.json" && echo "ID mapping complete"
```

### 4.5 U0.9 전용 two-DB 설정 생성

```bash
cp dreamplace/examples/two_db_superblue1.json "$CONFIG"

sed -i \
  -e 's/PLACEMENT_U0\.7/PLACEMENT_U0.7/g' \
  -e 's#"result_dir": "results/two_db_timing"#"result_dir": "results/two_db_timing_u07"#' \
  "$CONFIG"
```

경로가 올바르게 바뀌었는지 확인한다.

```bash
grep -E 'placement_db_path|placement_timing_mapping_path|result_dir' "$CONFIG"
```

다음 경로가 출력되어야 한다.

```text
results/superblue1/timing_flow_v2/PLACEMENT_U0.7/save
results/superblue1/timing_flow_v2/PLACEMENT_U0.7/ID_MAPPING
results/two_db_timing_u07
```

Inflation 효과만 비교하려면 우선 이 세 경로 외의 placement 설정은 기존 실행과
동일하게 유지한다.

### 4.6 Two-DB timing-driven placement 실행

전체 로그를 보존한다.

```bash
set -o pipefail

TC_CONFIG="$CONFIG" \
sh ./run_two_db_timing.sh 2>&1 | tee log_u07_full
```

예상 최종 DEF는 다음 위치에 생성된다.

```text
results/two_db_timing_u07/superblue1/superblue1.gp.def
```

로그에 parameter와 timing feedback이 남았는지 확인한다.

```bash
grep -E 'parameters =|Two-DB feedback|net-weighting|\[Final placement\]' log_u07_full | tail -80
```

### 4.7 빠른 geometry 비교

```bash
TC_PHYSICAL="$PHYSICAL" \
TC_TWO_DB_DEF="$RESULT/superblue1/superblue1.gp.def" \
TC_TWO_DB_LOG=log_u07_full \
TC_TWO_DB_CONFIG="$CONFIG" \
TC_COMPARE="$RESULT/comparison" \
sh ./run_compare_timing_placement.sh
```

결과:

```text
$RESULT/comparison/report.md
$RESULT/comparison/summary.json
$RESULT/comparison/net_comparison.tsv
```

### 4.8 동일 RC 조건의 STA 비교

```bash
TC_PHYSICAL="$PHYSICAL" \
TC_TWO_DB_DEF="$RESULT/superblue1/superblue1.gp.def" \
TC_TWO_DB_LOG=log_u07_full \
TC_TWO_DB_CONFIG="$CONFIG" \
TC_COMPARE="$RESULT/comparison_sta" \
sh ./run_compare_timing_placement.sh \
  --sta \
  --rc-r 2.535 \
  --rc-c 1.6e-16 \
  --ignore-net-degree 100 \
  --paths 10
```

결과:

```text
$RESULT/comparison_sta/report.md
$RESULT/comparison_sta/summary.json
$RESULT/comparison_sta/A_original.paths.json
$RESULT/comparison_sta/B_original_centers.paths.json
$RESULT/comparison_sta/C_two_db.paths.json
```

## 5. 결과 판단 기준

이전 U0.7 실행의 주요 기준값은 다음과 같다.

```text
External HPWL C/A       = 2.1731
Unclustered-only C/A    = 1.3923
C WNS                   = -37.76 ns
C TNS                   = -14.04 us
```

새 실행에서는 다음을 우선 확인한다.

1. `manifest.json`의 `achieved_utilization`이 목표와 가까운가.
2. `comparison_sta/report.md`에서 external HPWL C/A가 감소했는가.
3. `unclustered_only` C/A가 기존 `1.3923`보다 감소했는가.
4. C의 WNS/TNS가 기존 `-37.76 ns`, `-14.04 us`보다 개선됐는가.
5. `log_u08_full`에 실제 timing feedback과 net-weight update가 기록됐는가.

## 6. Original timing cache 호환성

이번 변경은 reduced placement LEF/DEF와 placement DB를 변경한다. 원본 timing
DB 자체는 다시 만들 필요가 없다. 다만 설치된 `timing_cpp` backend hash가 기존
timing cache manifest와 다르면 placement 실행 전에 timing model만 한 번 다시
내보내야 한다.

```bash
python3 dreamplace/TimingCache.py \
  dreamplace/examples/rebuild_superblue1_timing_cache.json \
  2>&1 | tee log_rebuild_timing_cache
```

`timing backend changed` 오류가 없으면 이 단계는 생략할 수 있다. 이 명령은 original
physical snapshot을 재생성하지 않고 동일한 original LIB/Verilog/SDC로 timing model만
갱신한다.

반면 reduced physical DB와 ID mapping은 LEF geometry가 바뀌므로 반드시 새로
생성해야 한다.

## 7. 구현 검증 상태

- 관련 단위/통합 테스트 44개 통과, 2개 환경 의존 테스트 skip
- 실제 superblue1 전체 LEF/DEF 생성 성공
- 생성된 complete `placement.lef`와 `reduced.def`로 physical DB 저장 성공
- 목표 `0.9`에서 fixed cell은 그대로 유지되고 movable 면적 배율 `1.1361283120` 적용 확인
