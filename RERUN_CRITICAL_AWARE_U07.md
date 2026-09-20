# Critical-aware clustering U0.7 전체 실행 가이드

이 문서는 `superblue1`에 새 critical-aware Leiden clustering을 적용하고,
utilization `0.7` 조건으로 placement LEF/DEF, placement DB, timing mapping,
two-DB timing-driven placement 및 동일 RC STA 비교까지 실행하는 전체 순서다.

## 1. 이번 실행에서 재사용하는 것과 새로 만드는 것

다음 원본 데이터는 기존 결과를 그대로 재사용한다.

- `cell_edges.tsv`
- `timing_edges.csv`
- `cell_names.tsv`
- `critical_cells.tsv`
- original timing DB와 timing cache
- 원본 LEF/DEF/Liberty

따라서 timing edge 추출이나 original placement부터 다시 시작하지 않는다.
**새 critical-aware Leiden clustering부터 시작한다.**

새로 생성되는 흐름은 다음과 같다.

```text
CriticalAwareLeidenCluster.py
  → leiden_critical_v1/clusters.tsv

ClusterPlacement.py
  → PLACEMENT_CRITICAL_U0.7/placement.lef
  → PLACEMENT_CRITICAL_U0.7/reduced.def

PreparePlacementDB.py
  → PLACEMENT_CRITICAL_U0.7/save

PlacementTimingMapping.py
  → PLACEMENT_CRITICAL_U0.7/ID_MAPPING

Placer.py (two-DB)
  → two_db_timing_critical_u07/superblue1/superblue1.gp.def

CompareTimingPlacement.py
  → comparison 및 comparison_sta
```

기존 결과는 보존하고, 이번 실행은 별도 경로를 사용한다.

| 구분 | 기존 | 이번 실행 |
|---|---|---|
| Clustering | `$FLOW/leiden` | `$FLOW/leiden_critical_v1` |
| Physical | `$FLOW/PLACEMENT_U0.7` | `$FLOW/PLACEMENT_CRITICAL_U0.7` |
| Result | `results/two_db_timing_u07` | `results/two_db_timing_critical_u07` |

## 2. 터미널 준비와 공통 변수

새 터미널을 열었다면 아래 블록부터 실행한다. 모든 후속 명령은 같은 터미널에서
실행해야 한다.

```bash
cd /mnt/hdd1/XP_timing_4.1/bin
set -o pipefail

export SRC=/mnt/hdd1/XP_timing_4.1/dreamplace
export FLOW=results/superblue1/timing_flow_v2
export TIMING_DB=results/superblue1/timing_edges_protected/save
export BENCH=benchmarks/iccad2015.ot/superblue1

export CLUSTER=$FLOW/leiden_critical_v1
export PHYSICAL=$FLOW/PLACEMENT_CRITICAL_U0.7
export CONFIG=$SRC/examples/two_db_superblue1_critical_u07.json
export RESULT=results/two_db_timing_critical_u07
export LOG=log_critical_u07_full
```

`LOG` 변수 끝에 한글 조사나 다른 문자를 붙이지 않는다. 설정값을 확인한다.

```bash
echo "$SRC"
echo "$CLUSTER"
echo "$PHYSICAL"
echo "$CONFIG"
echo "$RESULT"
echo "$LOG"
```

입력 파일이 존재하는지 확인한다.

```bash
for p in \
  "$SRC/CriticalAwareLeidenCluster.py" \
  "$SRC/ClusterPlacement.py" \
  "$FLOW/cell_edges.tsv" \
  "$FLOW/timing_edges.csv" \
  "$FLOW/cell_names.tsv" \
  "$FLOW/critical_cells.tsv" \
  "$TIMING_DB" \
  "$BENCH/superblue1.lef" \
  "$BENCH/superblue1_Late.lib" \
  "$BENCH/superblue1_withnets.def" \
  "$CONFIG"
do
  test -e "$p" || echo "MISSING: $p"
done
```

아무것도 출력되지 않으면 필요한 입력이 모두 존재한다.

## 3. 새 출력 경로 확인

각 생성기는 기존 출력 디렉터리를 덮어쓰지 않는다.

```bash
for p in "$CLUSTER" "$PHYSICAL" "$RESULT"
do
  if test -e "$p"; then
    echo "ALREADY EXISTS: $p"
  else
    echo "FREE: $p"
  fi
done
```

처음 실행이라면 모두 `FREE`여야 한다. 기존 결과가 있으면 무조건 삭제하지 말고
`leiden_critical_v2`, `PLACEMENT_CRITICAL_V2_U0.7`,
`two_db_timing_critical_v2_u07`처럼 새 이름을 사용한다. 이 경우 config 내부 경로도
같이 변경해야 한다.

## 4. 1단계: Critical-aware Leiden clustering

이 단계는 **어떤 원본 셀을 보호하고 어떤 셀들을 같은 cluster로 묶을지 결정**한다.
아직 placement LEF나 DEF를 만들지 않는다.

기본 정책은 다음과 같다.

- Slack 0 ps 미만 cell은 clustering하지 않는다.
- Critical cell의 양방향 fanin/fanout 2-hop을 보호한다.
- Fanout 32 이상 driver를 보호한다.
- Clock/reset net 연결 cell을 보호한다.
- Macro 10 µm 주변 cell을 보호한다.
- Slack을 알 수 없는 candidate를 보호한다.
- Slack 0~100 ps: 최대 4-cell cluster
- Slack 100~500 ps: 최대 16-cell cluster
- Slack 500 ps 이상: 최대 32-cell cluster
- 최종 cluster는 boundary pin 24개, 보수적 timing arc 64개 이하

실행 명령:

```bash
python3 "$SRC/CriticalAwareLeidenCluster.py" \
  --edges "$FLOW/cell_edges.tsv" \
  --timing-edges "$FLOW/timing_edges.csv" \
  --cell-names "$FLOW/cell_names.tsv" \
  --saved-db "$TIMING_DB" \
  --lib-dir "$BENCH/superblue1_Late.lib" \
  --critical-cells "$FLOW/critical_cells.tsv" \
  --protect-slack-below-ps 0 \
  --critical-hops 2 \
  --protect-fanout 32 \
  --macro-halo-um 10 \
  --slack-t1-ps 100 \
  --slack-t2-ps 500 \
  --max-cells-critical 4 \
  --max-cells-near 16 \
  --max-cells-noncritical 32 \
  --max-boundary-pins 24 \
  --max-timing-arcs 64 \
  --modularity-cutoff 0.8 \
  --resolution 1 \
  --seed 42 \
  --original-cycles retain \
  --output "$CLUSTER" \
  2>&1 | tee log_critical_leiden_v1
```

완료 여부를 확인한다.

```bash
test -f "$CLUSTER/summary.json" && \
test ! -f "$CLUSTER/FAILED_DO_NOT_USE.txt" && \
echo "critical-aware Leiden complete"
```

아래 파일들이 생성되어야 한다.

```bash
ls -lh \
  "$CLUSTER/clusters.tsv" \
  "$CLUSTER/cluster_stats.tsv" \
  "$CLUSTER/cell_assignments.tsv" \
  "$CLUSTER/critical_protection.tsv" \
  "$CLUSTER/summary.json"
```

정책과 cluster 수를 확인한다.

```bash
python3 -c "
import json
d = json.load(open('$CLUSTER/summary.json'))
p = d['critical_policy']
print('clusters           =', d['clusters'])
print('merged cells       =', d['merged_cells'])
print('retained eligible  =', d['retained_eligible_cells'])
print('newly protected    =', p['newly_protected_eligible_cells'])
print('remaining eligible =', p['remaining_eligible_cells'])
print('reason counts      =', p['protected_eligible_reason_counts'])
print('slack bands        =', p['slack_bands'])
"
```

기본 정책의 사전 분석에서는 913,269개 eligible cell 중 93,960개가 추가 보호되고
819,309개가 clustering 후보로 남았다. 실제 최종 cluster 수와 merged cell 수는
이번 실행의 `summary.json`을 기준으로 판단한다.

### 여기서 `run_cluster_placement.sh`를 실행하면 안 되는 이유

기존 `/mnt/hdd1/XP_timing_4.1/bin/run_cluster_placement.sh`는 다음 과거 membership을
고정해서 사용한다.

```text
$FLOW/leiden/clusters.tsv
```

이번에 사용해야 하는 것은 다음 파일이다.

```text
$FLOW/leiden_critical_v1/clusters.tsv
```

따라서 다음 단계에서는 기존 wrapper를 사용하지 않고 `ClusterPlacement.py`에 새
`clusters.tsv`를 직접 전달한다.

## 5. 2단계: U0.7 placement LEF와 reduced DEF 생성

이 단계는 1단계의 membership을 실제 placement용 cluster cell로 변환한다.

```bash
python3 "$SRC/ClusterPlacement.py" \
  --clusters "$CLUSTER/clusters.tsv" \
  --cell-names "$FLOW/cell_names.tsv" \
  --saved-db "$TIMING_DB" \
  --def-input "$BENCH/superblue1_withnets.def" \
  --lef-input "$BENCH/superblue1.lef" \
  --utilization 0.7 \
  --pin-layer metal2 \
  --output "$PHYSICAL" \
  2>&1 | tee log_cluster_placement_critical_u07
```

생성 파일을 확인한다.

```bash
test -f "$PHYSICAL/manifest.json" && \
test ! -f "$PHYSICAL/FAILED_DO_NOT_USE.txt" && \
echo "critical U0.7 LEF/DEF complete"

ls -lh \
  "$PHYSICAL/placement.lef" \
  "$PHYSICAL/reduced.def" \
  "$PHYSICAL/cluster_sizes.tsv" \
  "$PHYSICAL/inflated_masters.tsv" \
  "$PHYSICAL/cell_mapping.tsv" \
  "$PHYSICAL/pin_mapping.tsv" \
  "$PHYSICAL/net_mapping.tsv" \
  "$PHYSICAL/manifest.json"
```

Utilization 계산을 확인한다.

```bash
python3 -c "
import json
d = json.load(open('$PHYSICAL/manifest.json'))
print('target utilization  =', d['target_utilization'])
print('current utilization =', d['current_utilization'])
print('movable inflation   =', d['movable_area_inflation'])
print('achieved utilization=', d['achieved_utilization'])
print('clusters            =', d['counts']['clusters'])
print('original components =', d['counts']['original_components'])
print('reduced components  =', d['counts']['reduced_components'])
"
```

superblue1은 fixed cell을 포함한 현재 utilization이 약 0.7689다. Target 0.7이 현재
값보다 낮으므로 셀을 축소하지 않는다.

```text
expected movable_area_inflation = 1.0
```

Row/site 격자 올림 때문에 achieved utilization은 current utilization보다 조금 높을
수 있다. Fixed cell은 utilization 계산에는 포함되지만 크기가 바뀌지 않는다.

`placement.lef`는 complete replacement LEF다. Reduced DB를 만들 때 원본 LEF나
과거 cluster LEF를 같이 읽으면 안 된다.

## 6. 3단계: Reduced physical DB 생성

이제부터 기존 wrapper를 사용할 수 있다. `TC_PHYSICAL`로 새 경로를 명시한다.

```bash
TC_PHYSICAL="$PHYSICAL" \
sh ./run_prepare_placement_db.sh \
  2>&1 | tee log_prepare_db_critical_u07
```

완료 여부를 확인한다.

```bash
test -f "$PHYSICAL/save/physical_db/manifest.json" && \
test -f "$PHYSICAL/save/prepare_summary.json" && \
echo "reduced physical DB complete"
```

이 단계는 다음 두 파일만 physical input으로 사용해야 한다.

```text
$PHYSICAL/reduced.def
$PHYSICAL/placement.lef
```

## 7. 4단계: Original timing DB와 placement DB mapping 생성

```bash
TC_TIMING_DB="$TIMING_DB" \
TC_PHYSICAL="$PHYSICAL" \
sh ./run_placement_timing_mapping.sh \
  2>&1 | tee log_mapping_critical_u07
```

완료 여부를 확인한다.

```bash
test -f "$PHYSICAL/ID_MAPPING/manifest.json" && \
echo "timing/placement ID mapping complete"
```

Mapping 통계를 확인한다.

```bash
python3 -c "
import json
d = json.load(open('$PHYSICAL/ID_MAPPING/manifest.json'))
print(json.dumps(d, indent=2))
"
```

## 8. 5단계: Two-DB 전용 config 확인

이번 실행에서는 `sed`로 기존 config를 수정하지 않는다. 다음 전용 config를 그대로
사용한다.

```text
/mnt/hdd1/XP_timing_4.1/dreamplace/examples/two_db_superblue1_critical_u07.json
```

JSON 문법과 핵심 경로를 확인한다.

```bash
python3 -m json.tool "$CONFIG" >/dev/null && echo "config JSON valid"

grep -E \
  'placement_db_path|timing_db_path|placement_timing_mapping_path|result_dir' \
  "$CONFIG"
```

다음 경로가 나와야 한다.

```text
results/superblue1/timing_flow_v2/PLACEMENT_CRITICAL_U0.7/save
results/superblue1/timing_edges_protected/save
results/superblue1/timing_flow_v2/PLACEMENT_CRITICAL_U0.7/ID_MAPPING
results/two_db_timing_critical_u07
```

## 9. 6단계: Full placement 전 A/B abstraction 검사

이 단계가 중요하다. Two-DB placement를 오래 실행하기 전에 새 cluster membership의
원본 위치 근사 오차를 측정한다.

```text
A_original:
  원본 cell 위치와 실제 원본 pin offset

B_original_centers:
  원본 배치를 유지하고 cluster member/pin을 cluster 중심으로 투영

C_two_db:
  여기서는 아직 최종 placement가 아니므로 reduced.def의 초기 위치
```

실행:

```bash
TC_PHYSICAL="$PHYSICAL" \
TC_TWO_DB_DEF="$PHYSICAL/reduced.def" \
TC_TWO_DB_LOG="$RESULT/no_placement_log" \
TC_TWO_DB_CONFIG="$CONFIG" \
TC_COMPARE="$RESULT/preplacement_sta" \
sh ./run_compare_timing_placement.sh \
  --sta \
  --rc-r 2.535 \
  --rc-c 1.6e-16 \
  --ignore-net-degree 100 \
  --paths 10
```

결과를 확인한다.

```bash
sed -n '1,260p' "$RESULT/preplacement_sta/report.md"
```

이전 membership의 기준은 다음과 같았다.

```text
A WNS                  ≈ -13.76 ns
A TNS                  ≈  -7.43 µs
B WNS                  ≈ -55.47 ns
B TNS                  ≈ -32.75 µs
External HPWL B/A      ≈   1.88
```

새 결과에서는 B WNS/TNS 및 B/A가 이보다 개선되는지 확인한다. B가 거의 개선되지
않았다면 full placement가 cluster abstraction 손실을 모두 복구하기 어렵다. 이 경우
7단계를 바로 실행하기보다 cluster 크기 축소, 물리적 거리 제한 또는 중앙 virtual
pin model 개선을 먼저 고려한다.

정책을 변경하면 새 이름의 `CLUSTER`, `PHYSICAL`, `RESULT`로 4단계부터 다시 실행한다.

## 10. 7단계: Two-DB timing-driven placement 실행

6단계 결과를 확인한 뒤 실행한다.

```bash
TC_CONFIG="$CONFIG" \
sh ./run_two_db_timing.sh 2>&1 | tee "$LOG"
```

`set -o pipefail`을 앞에서 설정했으므로 `Placer.py`가 실패하면 pipeline도 실패 상태가
된다.

최종 DEF를 확인한다.

```bash
test -f "$RESULT/superblue1/superblue1.gp.def" && \
echo "two-DB placement complete"
```

실제 parameter와 timing feedback이 기록됐는지 확인한다.

```bash
grep -E \
  'parameters =|Two-DB feedback|net-weighting|\[Final placement\]' \
  "$LOG" | tail -100
```

### `timing backend changed` 오류가 발생한 경우

새 clustering 자체는 original timing backend를 변경하지 않는다. 하지만 현재 설치된
`timing_cpp`와 저장된 cache hash가 다르면 다음 오류가 날 수 있다.

```text
ValueError: timing backend changed; export again with db_option=def, mode=binary_write
```

그때만 timing cache를 다시 생성한다.

```bash
python3 dreamplace/TimingCache.py \
  dreamplace/examples/rebuild_superblue1_timing_cache.json \
  2>&1 | tee log_rebuild_timing_cache
```

Cache 생성 성공 후 7단계를 다시 실행한다. 실패한 결과와 혼동되지 않도록 필요하면
새 result/log 이름과 그에 맞는 새 config를 사용한다.

## 11. 8단계: 최종 geometry 비교

```bash
TC_PHYSICAL="$PHYSICAL" \
TC_TWO_DB_DEF="$RESULT/superblue1/superblue1.gp.def" \
TC_TWO_DB_LOG="$LOG" \
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

```bash
sed -n '1,240p' "$RESULT/comparison/report.md"
```

## 12. 9단계: 동일 RC 조건의 최종 STA 비교

```bash
TC_PHYSICAL="$PHYSICAL" \
TC_TWO_DB_DEF="$RESULT/superblue1/superblue1.gp.def" \
TC_TWO_DB_LOG="$LOG" \
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

```bash
sed -n '1,300p' "$RESULT/comparison_sta/report.md"
```

## 13. 최종 결과 판단 순서

다음 순서로 판단한다.

1. `B_original_centers` WNS/TNS
   - Cluster membership과 중앙 pin abstraction 자체의 손실
2. External HPWL `B/A`
   - 원본 위치에서 cluster가 외부 net geometry를 얼마나 왜곡했는지
3. `C_two_db` WNS/TNS
   - 새 placement의 최종 timing 결과
4. External HPWL `C/A`
   - Cluster boundary net의 최종 배치 품질
5. Unclustered-only HPWL `C/A`
   - Cluster 때문에 일반 셀 배치까지 나빠졌는지
6. Cluster 수와 reduced component 수
   - Timing 개선을 위해 메모리 절감 효과를 너무 많이 잃지 않았는지

이전 U0.7 membership의 동일 RC 기준은 다음과 같다.

```text
A WNS                   = -13.76 ns
A TNS                   =  -7.43 µs
B WNS                   = -55.47 ns
B TNS                   = -32.75 µs
C WNS                   = -36.51 ns
C TNS                   = -15.11 µs
External HPWL C/A       =   2.094
Unclustered-only C/A    =   1.402
```

새 결과는 우선 B가 개선되어야 하고, 그다음 C가 기존 U0.7보다 개선되어야 한다.

## 14. 파일 혼용 금지

새 membership을 사용했으므로 다음 파일은 항상 같은 세대끼리 사용한다.

```text
$CLUSTER/clusters.tsv
$PHYSICAL/placement.lef
$PHYSICAL/reduced.def
$PHYSICAL/save
$PHYSICAL/ID_MAPPING
$RESULT/superblue1/superblue1.gp.def
```

다음 과거 파일을 새 흐름에 섞지 않는다.

```text
$FLOW/leiden/clusters.tsv
$FLOW/PLACEMENT_U0.7/*
results/two_db_timing_u07/*
```

특히 새 physical DB에 과거 `ID_MAPPING`을 사용하거나, 새 mapping에 과거
`placement.lef/reduced.def`를 사용하는 것은 허용되지 않는다.

## 15. 한 줄 요약

```text
새 Leiden 생성
→ 새 clusters.tsv로 직접 ClusterPlacement 실행
→ 새 physical DB 생성
→ 새 ID mapping 생성
→ A/B 사전 STA 검사
→ two-DB placement
→ 동일 RC 최종 STA 비교
```
