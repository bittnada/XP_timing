# Critical-aware Leiden clustering

`CriticalAwareLeidenCluster.py`는 기존 `LeidenCluster.py`와 기존 Leiden 결과를
수정하지 않는 별도 실행 파일이다. 기존의 전체 연결 DAG, boundary pin,
output-dependency 검사는 그대로 재사용하고 그 앞에 timing 보호 정책과 slack별
크기 제한을 추가한다.

## 기본 정책

- `timing_slack_ps < 0`: clustering에서 제외
- 위 critical cell의 양방향 fanin/fanout 2-hop: clustering에서 제외
- fanout 32 이상 driver: clustering에서 제외
- `USE CLOCK` 또는 이름이 `clk|clock|reset|rst`와 일치하는 net의 cell: 제외
- non-CORE macro에서 10 µm 이내 cell: 제외
- finite slack이 없는 기존 candidate: 제외
- 서로 다른 scope 사이의 edge 양 끝 cell: `--scope-map`을 준 경우 제외
- `0 <= slack < 100 ps`: 최대 4 cells
- `100 <= slack < 500 ps`: 최대 16 cells
- `slack >= 500 ps`: 최대 32 cells

Cell slack은 `timing_edges.csv`에서 그 cell에 incident한 edge의 가장 나쁜 finite
`timing_slack_ps`로 정의한다. Slack band와 launch/capture domain이 모두 같은
cell만 같은 후보 cluster에 들어갈 수 있다. Adaptive cap 분할 이후에도 기존
Leiden repair가 max boundary pin, timing arc, cycle, output dependency를 검사한다.

현재 `superblue1.v`는 flatten되어 있으므로 hierarchy 보호는 자동으로 만들 수 없다.
별도의 scope mapping이 있을 때만 아래 형식으로 전달한다.

```text
cell_id scope
123     top/U_CPU/U_ALU
124     top/U_CPU/U_ALU
900     top/U_MEM/U_CTRL
```

## superblue1 실행

기존 `leiden` 디렉터리는 그대로 두고 반드시 새 output을 사용한다.

```bash
cd /mnt/hdd1/XP_timing_4.1/bin/results/superblue1/timing_flow_v2

python3 /mnt/hdd1/XP_timing_4.1/dreamplace/CriticalAwareLeidenCluster.py \
  --edges cell_edges.tsv \
  --timing-edges timing_edges.csv \
  --cell-names cell_names.tsv \
  --saved-db ../timing_edges_protected/save \
  --lib-dir /mnt/hdd1/XP_timing_4.1/bin/benchmarks/iccad2015.ot/superblue1/superblue1_Late.lib \
  --critical-cells critical_cells.tsv \
  --protect-slack-below-ps 0 \
  --critical-hops 2 \
  --protect-fanout 32 \
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
  --output leiden_critical_v1
```

`--scope-map hierarchy_scope.tsv`는 실제 hierarchy mapping을 확보했을 때만 추가한다.
Control-net 이름 규칙이 설계와 다르면 `--control-net-regex`를 변경한다. 빈 문자열을
주면 control-net scan을 끈다. Macro halo를 끄려면 `--macro-halo-um 0`, missing slack
보호를 끄려면 `--no-protect-missing-slack`을 사용한다.

## 추가 출력

기존 Leiden 출력에 다음 정보가 추가된다.

| 파일/필드 | 내용 |
| --- | --- |
| `critical_protection.tsv` | 보호된 cell ID, 이름, worst incident slack, 보호 이유 |
| `summary.json.critical_policy` | 이유별 보호 수, band별 후보 수와 cap, policy 옵션 |
| `cell_assignments.tsv` | 기존과 동일한 전체 최종 membership 감사표 |

보호 이유는 중복될 수 있으므로 이유별 count의 합이 고유 보호 cell 수와 같을 필요는
없다. `newly_protected_eligible_cells`가 실제로 이 정책 때문에 추가로 원본 유지된
cell 수다.

## 다음 단계

새 membership으로 LEF/Liberty/Verilog/DEF와 mapping을 모두 다시 생성해야 한다.
기존 `leiden/clusters.tsv`로 만든 산출물과 혼용하지 않는다. Placement 실행 전에
원본 위치 비교의 `B_original_centers`를 먼저 계산하여 A 대비 external HPWL과
WNS/TNS가 개선됐는지 확인한다.

## 테스트

```bash
cd /mnt/hdd1/XP_timing_4.1/dreamplace
python3 -B -m unittest test_critical_aware_leiden test_leiden_cluster -v
```
