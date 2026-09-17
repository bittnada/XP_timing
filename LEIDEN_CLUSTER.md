# Leiden 후보 생성 및 비순환 클러스터 보정

`LeidenCluster.py`는 기존 weighted cell edge를 이용해 **실제 Leiden**을 실행하고,
축약 그래프의 순환과 크기·pin 예산 위반을 관련 클러스터의 분할로 보정한다.
초기 STA를 다시 실행하지 않는다. `TimingCluster.py`의 greedy 알고리즘은 변경하지 않는다.
`--initial-clusters`가 있으면 기존 membership을 후보로 삼아 보정만 수행한다.

## 실행: 새 Leiden 결과

```bash
cd /mnt/hdd1/XP_timing_4.1/bin/results/superblue1/timing_edges_protected
python3 /mnt/hdd1/XP_timing_4.1/bin/dreamplace/LeidenCluster.py \
  --edges cell_edges.tsv \
  --timing-edges timing_edges.csv \
  --cell-names cell_names.tsv \
  --saved-db save \
  --lib-dir /mnt/hdd1/XP_timing_4.1/bin/benchmarks/iccad2015.ot/superblue1/superblue1_Late.lib \
  --critical-cells critical_cells.tsv \
  --max-cells 32 \
  --max-boundary-pins 24 \
  --max-timing-arcs 64 \
  --seed 42 \
  --output leiden_acyclic
```

원본 Liberty는 FF/latch/statetable 판별에 사용하며 timing LUT 계산은 하지 않는다.
한 Liberty 파일, 여러 파일(`--lib-dir` 반복), 하위 폴더를 포함한 Liberty 디렉터리를
지원한다. 기존 `.lib` 경로는 `bin/benchmarks/...`에 있는지 확인한다.
연결에 등장하는 모든 원본 셀 master의 Liberty 정의가 필요하다.

**현재 superblue1 저장 연결에서는 FF를 분리한 원본 셀 수준 그래프에도 순환이
검출되었다.** 기본값은 이를 오류로 중단한다. 원래 순환 영역은 그대로 유지하고
그 밖의 clustering을 진행하려면 아래의 의미를 확인한 뒤 명시적으로
`--original-cycles retain`을 추가한다.

## 실행: 기존 93,399개 클러스터 보정만

위 명령에 다음 옵션을 추가하고 새 출력 디렉터리를 지정한다.

```bash
--initial-clusters superblue1_supercell_clustered.txt \
--output repaired_clusters
```

이는 여러 무작위 Leiden 결과를 만들어 선별하는 방식이 아니다. 입력 partition
하나에서 시작해 문제가 있는 부분을 나눈다. 후보에 새로 보호된 셀 등이 들어
있으면 그 셀은 병합에서 제외하고 원본으로 유지하며 개수를 summary에 기록한다.
초기 파일에 없는 eligible 셀도 임의로 기존 클러스터에 추가하지 않고 singleton으로 시작한다.
입력의 cluster_id는 감사 표의 `input_cluster_id` 열에 보존한다.
summary의 `candidate.internal_weight_fraction`은 후보 내부에 남는 eligible edge
weight 비율이다. 기존 membership에서 이 비율이 1% 미만이면 ID 공간 확인 경고를
표시한다. 외부 Leiden 코드가 만든 내부 vertex 번호를 원본 cell_id로 착각하지
않도록 주의한다. 프로그램이 원본 ID를 추측해 자동 변경하지는 않는다.

## 두 그래프와 ID

| 입력 | 역할 |
| --- | --- |
| `--edges` | `driver_id sink_id weight`: Leiden 목적 함수. 기본 TSV/CSV/공백 표 지원 |
| `--timing-edges` | timing_edges.csv/TSV: 후보 셀 및 launch/capture domain mask |
| `--cell-names` | **위 파일들과 동일한 ID 공간**의 cell_id/cell_name 표 |
| `--saved-db` | cells_info, lef_info, netlist_info, ext_pin_info JSON |
| `--lib-dir` | 원본 Liberty 정의에서 순차 셀 식별 |
| `--critical-cells` | 병합 제외 셀. 생략 시 timing_edges 옆 critical_cells.tsv |

MakeDB의 node_names 순서와 edge ID 순서는 다를 수 있으므로 이름으로 join한다.
timing_edges에서 같은 ID가 다른 이름이거나 domain mask가 서로 다르면 중단한다.
가중치 edge가 원본 연결에 없으면 stale 입력으로 판단하여 중단한다.
다른 domain, 고정 셀, macro, 순차 셀, critical 셀의 weight edge는 사용하지 않는다.

가중치 그래프는 양방향 affinity로 합산하여 RBConfiguration 목적 함수의 Leiden에
사용한다. 순환 검사는 별도의 **전체 원본 연결 방향 그래프**에서 수행한다.
낮은 weight, 원본 cell_edges에서 빠진 연결, critical 셀을 통과하는 경로도 유지한다.
비후보 셀을 모두 삭제한 induced graph만 검사하는 방식은 사용하지 않는다.

FF/latch/statetable 셀은 입력 vertex와 출력 source vertex를 분리하고 D→Q 연결을
만들지 않는다. PI/PO도 방향에 맞게 추가한다. 보호된 조합 셀은 통과 경로를
보존한 singleton이다. Macro의 내부 경로는 셀 수준으로 보수적으로 연결한다.
원본 constraint graph부터 순환이면 성공 처리하지 않고 관련 vertex 예와 함께 중단한다.
이는 실제 timing loop 또는 보수적인 셀 모델/메타데이터의 문제일 수 있다.

### 원본 그래프에 이미 순환이 있는 경우

`--original-cycles error`가 기본값이다. 클러스터를 풀어도 원래 있던 순환은 없앨
수 없으므로 이 경우 입력/모델 확인을 요구한다.

명시적 `--original-cycles retain`은 원본 순환 SCC 안의 모든 셀을 병합 대상에서
제외하고 **원본 셀/원본 net 그대로 유지**한다. 검사 그래프에서만 원래 SCC를
하나의 변경 불가 vertex로 표현하여 나머지 clustering이 추가 순환을 만들지
않게 한다. 실제 회로 net을 끊거나 이 SCC를 reduced cell로 생성하지 않는다.
이때의 보장은 **원본 SCC를 축약한 검사 그래프의 DAG**이지 원본 회로 전체에
순환이 없다는 보장이 아니다. summary의 `original_dag=false`, `dag_scope`,
`original_cyclic_sccs`와 감사 표의 `retained_original_cycle`에 이를 명시한다.

clock-tree/clock-domain eligibility는 **제공한 timing_edges의 기존 분석 결과**를
재사용한다. 별도 clock parser나 STA로 다시 인증하지 않는다. unknown/multi-domain
mask는 거부하며 서로 다른 launch/capture mask 조합은 같은 그룹으로 묶지 않는다.
critical 표가 없으면 명시적인 `--no-critical-protection` 없이는 실행하지 않는다.

## 보정 알고리즘

1. 원본 전체 방향 그래프가 DAG인지 검사하고 topological rank를 구한다.
2. Leiden 후보 또는 `--initial-clusters` partition을 가져온다.
3. 서로 다른 domain은 분리하고, 그룹 내부의 weakly connected component도 분리한다.
4. 원본 연결로 축약 그래프를 만들고 SCC를 검사한다. 2개 이상 노드가 있는 SCC가
   순환 영역이다. 단순히 양방향 pair만 검사하지 않는다.
5. 순환 영역, 크기/pin 예산 위반, 경계 출력 간 내부 의존이 있는 multi-cell 그룹을 분할한다.
6. 전체 그래프를 다시 검사한다. 통과할 때까지 반복한다.

분할은 그룹 멤버를 원본 topological rank로 정렬한 다음, 가운데 약 1/3~2/3 위치의
절단점 중 **끊기는 내부 weight 합이 최소인 위치**를 선택한다. 동점이면 더 균형
잡힌 위치를 선택한다. 하나의 클러스터를 둘로 나누는 휴리스틱이며 전역 최적화나
한 번에 순환 제거를 보장하지 않는다. 분할 후 다시 연결성·순환·예산을 검사한다.
Leiden을 각 단계마다 재실행하지 않으며, 서로 다른 후보를 새로 합치지도 않는다.

`--repair-rounds` 기본 12회 이후에도 문제가 있는 그룹은 singleton으로 푼다.
모든 보정은 split-only이고 원본 검사 그래프가 DAG이므로 singleton fallback으로 종료할
수 있다. retain 모드에서는 원본 SCC를 변경 불가 vertex로 축약한 그래프를 뜻한다.
최종 축약 검사 그래프의 위상정렬을 다시 검증한 후에만 완료 결과를 저장한다.
SCC 전체를 거대한 cluster 하나로 합치는 동작은 하지 않는다.

### 경계 출력 간 내부 의존 검사 (항상 적용)

현재 ReducedLiberty의 출력별 독립적인 2D slew/load 표와 맞추기 위해,
경계 출력 net에서 클러스터 내부를 거쳐 다른 경계 출력까지 도달하는 그룹을
분할한다. 예를 들어 `NAND → INV → INV(O0) → INV(O1)`에서 O0와 O1이 둘 다
외부로 나가면, O0의 외부 부하가 O1의 지연에도 영향을 주므로 위반이다.
O0 뒤쪽의 내부 셀을 별도 그룹으로 분리하고 앞쪽 셀들은 함께 유지하려고 한다.
분할 뒤에는 새 경계 출력이 생길 수 있으므로 연결성·순환·크기와 이 조건을 모두
반복 검사한다. 출력 수가 여러 개라는 이유만으로 분할하지는 않는다.

검사는 실제 boundary net의 **내부 sink**에서 출발하며 같은 cluster 안의
cell-level 방향 경로만 따라간다. 여러 출력 핀이 있는 셀에서도 외부로 나가는
출력과 다른 출력의 fanout을 시작점에서 혼동하지 않는다. 이후 셀 내부에서는
각 입력이 모든 출력에 영향을 줄 수 있다고 보수적으로 가정한다. 정확한
Liberty pin별 timing arc를 활용하면 허용할 수 있는 일부 그룹도 분할할 수 있다.
원본 순환 SCC와 FF 등 비후보 원본 셀은 검사·분할 대상이 아니며 그대로 유지한다.

`repair_history[].output_dependency_violations`와 로그의 `output_dependencies`는
해당 라운드에서 검출한 위반 그룹 수다. 여러 라운드에 같은 원본 그룹의 일부가
등장할 수 있으므로 합계를 고유한 원본 cluster 개수로 해석하지 않는다.
`summary.json.boundary_output_independence`는 최종 multi-cell 그룹의 검증 결과다.
이 검사는 모든 Liberty 특성화 조건을 보장하지 않는다. Mixed-polarity 재수렴,
conditional/non-unate arc 등은 ReducedLiberty의 기존 검사를 계속 통과해야 한다.

이미 만든 partition에 새 검사만 적용하려면 기존 실행 명령에서
`--modularity-cutoff`를 빼고 다음을 지정한다. 다른 입력과 크기 제약은 유지한다.

```bash
  --initial-clusters leiden_recursive_q07/clusters.tsv \
  --output leiden_libsafe
```

이 모드는 Leiden을 다시 실행하지 않고 기존 그룹을 필요한 만큼만 분할한다.
최종 cluster ID가 재배정되므로 새 membership과 크기표로 LEF·Verilog·Liberty·DEF를
다시 생성해야 한다. 이전 partial Liberty 디렉터리를 이어쓰기하거나 섞지 않는다.

## 재귀적 modularity cutoff 모드

`--modularity-cutoff 0.7`을 주면 `/tmp/leiden_cpu-main/leiden_cpu/main.cxx`와
`scripts/create_clusters_threads.py`의 조합을 참고한 재귀 모드를 사용한다.
옵션을 생략하면 기존 한 번의 Leiden + legality repair 동작을 유지한다.

1. eligible 셀을 launch/capture domain별로 분리한 weighted graph에서 시작한다.
2. 현재 induced graph에 Leiden을 실행해 후보 community들을 찾는다.
3. 현재 graph의 weight 총합을 기준으로 정규화한 weighted modularity Q를 계산한다.
4. **Q < cutoff**이면 현재 graph 전체를 하나의 leaf 후보로 유지한다.
   이번 Leiden이 제안한 하위 community들을 최종 결과로 채택하는 것이 아니다.
5. **Q >= cutoff**이고 community가 둘 이상이면 각 community 내부 graph에서 반복한다.
   community가 하나이거나 singleton이면 종료한다. Edgeless graph는 singleton으로 유지한다.
6. 모든 leaf 후보에 기존 연결성/domain/순환/크기/boundary/arc 검사를 적용하고
   필요한 경우 추가 분할한다. 따라서 최종 partition은 cutoff leaf보다 잘게 나뉠 수 있다.

참고 C++의 `getModularity()`처럼 **cutoff 비교 Q의 resolution은 항상 1**이다.
`--resolution`은 후보 분할을 찾는 RBConfiguration 목적 함수의 설정이며 별개다.
따라서 일반적인 사용은 `--resolution 1`을 권한다. 참조 구현과 재귀/정지 의미는
같지만 backend는 기존 Python igraph/leidenalg이며 C++의 OpenMP, 반복 횟수,
난수 처리와 같지 않으므로 동일한 partition이나 속도를 보장하지 않는다.

참고 후처리 스크립트는 leaf TSV 파일 하나를 하나의 cluster로 취급한다.
여기서는 중간 파일 재번호화 없이 local graph index→원본 physical node→cell_id를
명시적으로 보존한다. 그래프 내부 index를 원본 cell_id로 잘못 사용하는 것을 피한다.

예: 기존 실행 명령에 다음 옵션을 추가하고 출력 경로를 새로 지정한다.

```bash
  --modularity-cutoff 0.7 \
  --resolution 1 \
  --max-cells 128 \
  --max-boundary-pins 64 \
  --max-timing-arcs 256 \
  --output leiden_recursive_q07
```

`--max-cells 0`은 셀 수 제한을 해제한다. 재귀 모드에서는 셀 수 제한을 Leiden의
`max_comm_size`에 넣지 않고, leaf 후보에 대한 legality repair에서만 적용한다.
이는 크기 상한이 Q 자체를 바꾸지 않게 하기 위함이다. Pin/arc/area 및 순환 검사는
그대로 적용되며, 큰 그룹은 timing characterization 비용이 증가할 수 있다.
`--initial-clusters`와 cutoff를 함께 사용하는 것은 오류다.

`modularity_tree.tsv`에 tree_id, parent_id, depth, cells, edges, modularity,
communities, decision을 기록한다. Singleton/edgeless Q는 정의하지 않고 빈칸으로 쓴다.
`summary.json`의 candidate에는 cutoff, score 정의, 정지 사유별 개수, 최대 깊이,
leaf 수를 저장한다. Tree ID는 검사 이력용이며 최종 cluster ID와 다르다.
기존 산출물은 자동 변경되지 않으며 최종 membership으로 LEF/Liberty/Verilog/DEF를
다시 생성해야 한다. 이 옵션은 제외된 셀을 후보로 복구하거나 목표 인스턴스 수를
보장하지 않는다.

## 크기/pin 제약 및 출력

- `--max-cells 32`: 그룹당 최대 셀 수, 0이면 제한 없음. 한 번 실행 모드에서는
  Leiden 후보 생성에도 적용하며 재귀 cutoff 모드에서는 최종 repair에만 적용한다.
- `--max-boundary-pins 24`: 정확한 외부 input net 수 + output net 수.
- `--max-timing-arcs 64`: input 수 × output 수. 실제 도달 가능한 arc 수가 아닌 보수적 상한.
- `--max-area`: 원본 셀들의 LEF 면적 합(µm²). 기본 제한 없음.
- 경계 input 또는 output이 없는 multi-cell 그룹도 분할한다.
- `--resolution 1.0`, `--iterations 2`, `--seed 42`: Leiden 실행 설정.

출력 디렉터리는 존재하면 덮어쓰지 않는다. 원본 파일, 기존 클러스터, 기존
reduced LEF/Liberty/Verilog/DEF를 수정하지 않는다.

| 파일 | 내용 |
| --- | --- |
| `clusters.tsv` | **최종 multi-cell cluster만**: cell_id, cluster_id |
| `candidate_clusters.tsv` | 보정 전 eligible 셀의 후보 partition |
| `cell_assignments.tsv` | 입력 ID 표의 모든 셀, 후보/최종 cluster와 원본 유지 여부 |
| `cluster_sizes.tsv` | 최종 클러스터별 width/height, **µm** |
| `cluster_stats.tsv` | 셀 수, 면적, 경계 pin 수, 보수적 timing arc 수 |
| `summary.json` | 완료 상태, 입력, backend 설정, 보정 이력, DAG 검사 결과 |

최종 cluster_id는 최소 원본 cell_id 순서로 1부터 새로 매긴다. 예전 cluster ID나
예전 크기표를 그대로 사용하지 않는다. `cell_assignments.tsv`의 최종 cluster_id=-1은
**셀 삭제가 아니라 원본 셀 유지**다. ReducedVerilog는 membership에 없는 셀을 그대로
남기므로 fallback singleton은 불필요한 새 Liberty master로 만들지 않는다.
멀티셀 클러스터가 0개라면 원본 netlist를 사용하며 reduced 모델 생성은 불필요하다.

크기는 `면적합 / --utilization`을 footprint로 삼아 `--aspect-ratio`(width/height)에
맞춰 추정한다. 두 기본값은 1.0이다. 실제 row/site/grid 정렬이나 legalization은
하지 않으므로 필요하면 이 새 membership에 맞춘 크기를 별도로 제공한다.

## ReducedLEF 및 이후 흐름

```bash
python3 /mnt/hdd1/XP_timing_4.1/bin/dreamplace/ReducedLEF.py \
  --input repaired_clusters/clusters.tsv \
  --cell-names cell_names.tsv \
  --saved-db save \
  --sizes repaired_clusters/cluster_sizes.tsv \
  --pin-layer metal2 \
  --output repaired_clusters/cluster_cells.lef
```

이후 새 membership으로 reduced Liberty도 다시 characterization하고, 새 mapping으로
ReducedVerilog/ReducedDEF를 생성한다. 이전 mapping/Liberty와 섞어 사용하지 않는다.
DAG 통과는 sparse timing arc, polarity, reconvergence, output-to-output load 의존성,
SDC 예외 처리 등 ReducedLiberty의 모든 조건을 통과한다는 보장은 아니다.
이러한 모델 제약은 reduced Liberty 생성 시 별도로 검증한다.

## 의존성 및 검증

현재 프로젝트에는 igraph 0.11.9, leidenalg 0.10.2를 시스템 환경과 분리하여
`bin/dreamplace/_vendor/leiden`에 설치했다. 코드가 일반 Python 환경 또는 이 경로에서
backend를 찾는다. 새 설치 경로에는 다음과 같이 설치할 수 있다.

```bash
python3 -m pip install --target /path/bin/dreamplace/_vendor/leiden \
  igraph==0.11.9 leidenalg==0.10.2
```

NumPy/SciPy는 기존 DREAMPlace Python 환경을 사용한다. Leiden API는
[공식 문서](https://leidenalg.readthedocs.io/en/stable/reference.html)의 find_partition을 사용하며,
순환 보정은 본 프로젝트의 별도 로직이다.

```bash
cd /mnt/hdd1/XP_timing_4.1/dreamplace
OMP_NUM_THREADS=1 python3 -m unittest test_leiden_cluster -v
```

대규모 설계의 전체 CSR 그래프, Leiden graph 및 mapping을 메모리에 유지한다.
JSON은 항목별로 읽지만 전체 작업은 out-of-core 알고리즘이 아니다.
