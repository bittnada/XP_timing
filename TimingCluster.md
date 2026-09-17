# TimingCluster.py 상세 설명: timing-aware clustering과 Leiden 설계

작성 기준: 2026-09-16, 이 저장소의 실제 Python 구현.

이 문서는 대규모 standard-cell 설계를 축약하여 DREAMPlace/OpenTimer의 반복 계산을 줄이면서, 좋은 macro 배치 후보를 **생성**하는 데 사용할 timing-aware clustering을 설명한다. Macro 후보의 순위를 매기는 별도 모델에 대한 설명은 아니다.

가장 중요한 구분은 다음과 같다.

> `TimingCluster.py` 자체에는 Leiden 실행 코드가 없다. **2026-09-17에 독립 실행용
> `LeidenCluster.py`와 `LeidenGraph.py`를 추가했다.** 기존 timing edge와 MakeDB 저장
> 정보를 읽어 실제 Leiden 후보 생성, 전체 방향 그래프 SCC 검사, 국소 분할·재검사,
> singleton fallback을 수행한다. 실행법과 실제 보장 범위는 [LEIDEN_CLUSTER.md](LEIDEN_CLUSTER.md)를
> 우선 참고한다. 아래의 Leiden 설계 절은 초기 설계 배경이며 모든 제안이 구현된 것은 아니다.

### 원본 순환 셀 보호 기능

`detect_cyclic_cells()`가 cell→net→cell 방향 그래프의 SCC를 검사한다.
FF/latch에서는 경로를 끊으며, 나머지 physical cell은 후보 여부나 domain,
critical 보호 여부에 관계없이 검사에 포함한다. INOUT/알 수 없는 방향은
보수적으로 양방향 처리하므로 pin별 실제 timing loop 판정과는 다르다.

- `cyclic_cells.tsv`: cell_id, cell_name, scc_id, scc_cells,
  was_cluster_candidate를 cell_id 순서로 기록한다. 구분자는 탭이다.
- `cyclic_cells_summary.json`: SCC 개수, 보호 셀 수, 검사 모델을 기록한다.
- 순환 셀이 driver 또는 sink인 timing edge를 제외하며 원본 fanout은 유지한다.
- `--edges-only`와 직접 `build_timing_edgelist()` 호출에서도 자동 적용된다.
- 원본 셀/net/save는 삭제하지 않으며 greedy 병합에서도 해당 셀을 제외한다.

기존 edge 파일은 자동 변경되지 않는다. 새 디렉터리에 edge를 다시 생성하고
cell-edge 집계와 Leiden을 다시 실행해야 한다. `LeidenCluster.py` 수정은 필요
없지만 **축약으로 새로 생기는 순환 검사**는 여전히 필요하다. 또한 원본 save에는
기존 순환이 남으므로 전체 그래프 검사에 `--original-cycles retain`이 필요할 수 있다.

### 초기 critical path 셀 보호 기능

초기 STA 후 `extract_critical_cells()`가 **slack이 가장 작은 MAX/setup 경로 100개**를
기본 선택하고, 그 경로에서 실제로 통과한 셀을 저장한다. 음수 slack만 선택하는 것이
아니므로 모든 slack이 양수여도 상대적으로 가장 여유가 작은 경로를 보호한다.
이는 전체 critical path의 완전한 열거가 아니라 **top-K 경로 기반 보호**이다.
Rise/fall 경로는 별도로 집계될 수 있어 K개의 서로 다른 endpoint를 뜻하지 않는다.

```text
원본 연결에서 FF/macro/clock anchor와 domain 계산
→ 초기 STA → 실제 path pin/cell 조회 → critical 셀 집합 저장
→ driver 또는 sink가 보호 셀이면 edge 제외
→ 남은 timing_edges.csv → cell edge 집계 → 추후 Leiden
```

- `--critical-paths K`: 선택 경로 수. 기본 100, 0은 명시적으로 보호 기능을 끈다.
- `--critical-slack-ps S`: 위 K개 중 path slack ≤ S ps인 경로만 선택한다.
  예를 들어 0은 slack이 0 이하인 경로만 선택한다. **K 제한을 없애는 옵션은 아니다.**
- JSON 설정은 `timing_cluster_critical_paths`, `timing_cluster_critical_slack_ps`이며
  후자의 기본값 `null`은 slack 상한이 없다는 뜻이다.

출력은 다음과 같다.

| 파일 | 의미 |
|---|---|
| `critical_cells.tsv` | cell_id 순서의 셀 이름, 최악의 선택 경로 slack(ps), 선택 경로 포함 횟수, 원래 cluster 후보 여부 |
| `critical_paths.jsonl` | 경로별 slack, 실제 pin 순서, cell 이름/ID, rise/fall, arrival(ps) |
| `critical_cells_summary.json` | 선택 정책, 보호 셀 수, 기존 anchor 수, 경로 예산으로 추가 경로가 누락되는지 |
| `timing_edges.csv` | 보호 셀이 양 끝점에 없는 pin edge들 |

`critical_cells.tsv`의 slack은 **해당 셀을 지나는 선택 경로들의 최소 slack**이다.
기존 timing_edges의 sink-pin slack과 정의가 다르다. FF/macro가 선택 경로에 실제
포함되면 표에도 저장하지만, 이들은 원래부터 cluster 대상이 아니었다. Clock-tree
보호는 기존 clock-domain 추적을 유지한다. Primary IO point는 path JSONL에
`cell_id: null`로 기록한다. Net의 다른 fanout sink는 실제 경로에 없으면 자동으로
critical 셀이 되지 않는다. 이를 위해 기존 net ID 전용 API와 별도로
`Timer.report_timing_paths()` 및 C++ binding을 추가했다.

Fixed macro는 PlaceDB에서 `<원래 이름>.DREAMPlace.Shape0`, `Shape1` 등의
physical shape로 분할될 수 있다. `_resolve_timing_cell()`은 먼저 정확한 셀 이름을
조회하고, 이름이 없으면 **실제 경로 pin → pin2node_map → fixed shape**로 연결한다.
해당 shape의 이름이 원래 셀 이름과 정확한 `.DREAMPlace.Shape<숫자>` 접미사로
구성됐는지 확인한다. 단순히 Shape0을 추측하거나 이름 prefix만으로 연결하지 않는다.
`critical_cells.tsv`의 기존 `cell_id`/`cell_name`은 physical node ID/이름을 유지하고,
추가 열 `timing_cell_name`에 원래 OpenTimer 이름, `mapping_method`에 `exact_name`
또는 `fixed_shape_pin`을 저장한다. 경로 JSONL도 원래 `cell`과 대응 physical 이름을
함께 기록한다. Summary의 `fixed_shape_mapped_cells`는 이 방식으로 연결한 node 수다.
기하 도형을 별도 timing cell로 세지 않으며 pin을 소유한 대표 shape만 기록한다.
나머지 fixed shape도 원래부터 clustering 대상이 아니다.

보호 셀은 원본 netlist/STA/배치에서 삭제하거나 fixed로 만드는 것이 아니다.
Cluster 병합에서만 제외한다. 따라서 fanout은 보호 셀과 FF를 포함한 **원래 전체
sink pin 수**를 사용한다. Domain 전파와 topology 계산에서도 원본 구조를 유지한다.
Greedy에서는 보호 셀을 topological order의 병합 중단점으로 취급하고 assignment를
-1로 남긴다. 보호 셀을 먼저 graph에서 삭제하면 `A → critical → B`를 가로지르는
잘못된 `{A,B}` 병합이 가능하기 때문이다. Leiden 후 legality 검사도 이 원칙을
따라 원본 directed graph에서 수행해야 한다.

보호 기능이 켜진 상태에서 STA 경로가 전혀 없거나 경로 셀을 PlaceDB에서 찾지
못하면 오류로 중단한다. 보호 기능이 조용히 생략되지 않게 하기 위한 동작이다.
한 개의 추가 경로를 조회해 예산 초과 여부를 summary의 `path_budget_limited`에
기록한다. 초기 배치 RC 없는 setup 분석이므로 hold, 다른 corner/mode, 배치 후
새 critical path까지 보장하지 않는다. Reduced 모델 생성 시 외부 membership 파일에도
보호 셀을 넣지 않아야 한다. Converter는 membership을 따르며 별도로 이 파일을 읽지 않는다.

```bash
cd /mnt/hdd1/XP_timing_4.1/bin
python3 dreamplace/TimingCluster.py test/iccad2015.ot/superblue1.json \
  --edges-only --critical-paths 100 \
  --output results/superblue1/timing_edges_protected
```

기존 결과는 자동으로 바뀌지 않는다. 위처럼 새 디렉터리에 재생성한 다음 cell-edge
집계를 다시 실행한다. 이미 만든 cluster에서 나중에 셀을 빼는 방식이 아니라,
처음부터 보호 셀을 병합 후보에서 제외하는 흐름이다.

## 목차

1. 목표와 현재 구현 상태
2. cell, pin, net, driver, sink의 관계
3. 반드시 지켜야 하는 제약과 품질 개선용 기준
4. clock domain과 sequential boundary
5. topological order와 현재 greedy 알고리즘
6. 초기 STA, slack, fanout 및 timing weight
7. pin edge를 cell edge로 합치는 방법
8. Leiden을 어디에, 어떻게 사용하는가
9. Leiden 결과의 legality 검사와 분할
10. boundary pin과 reduced timing arc
11. reduced Liberty·Verilog·OpenTimer의 연결
12. 현재 코드의 함수·데이터·파일·실행 방법
13. 알려진 한계와 다음 구현 순서
14. 실험 지표와 최종 체크리스트

## 1. 목표와 현재 구현 상태

### 1.1 왜 clustering을 하는가?

Standard cell이 많으면 배치 변수, net/pin 처리, 배선 RC 구성, 타이밍 전파에 드는 비용이 커진다. 여러 조합논리 셀을 하나의 cluster로 나타내면 반복 계산에 필요한 객체 수를 줄일 수 있다.

그러나 단순히 셀 개수만 줄이면 다음 문제가 생길 수 있다.

- 실제 critical path를 잘못 표현한다.
- clock/sequential 경계가 사라진다.
- 외부로 나갔다가 다시 들어오는 경로 때문에 축약 그래프가 부적절해진다.
- cluster의 입출력 핀과 timing arc가 많아져, 셀 수는 줄어도 계산량은 충분히 줄지 않는다.
- cluster 내부 배선 지연을 무시한 오차가 커진다.
- 큰 cluster가 물리적 배치 자유도를 너무 제한한다.

따라서 목표는 **셀 수의 최대 축소**가 아니라 다음의 균형이다.

```text
계산 비용 감소 + 논리/타이밍 경계 보존 + 충분한 배치 자유도 + 허용 가능한 모델 오차
```

### 1.2 기능별 현재 상태

| 기능 | 현재 상태 | 담당 파일 |
|---|---|---|
| PlaceDB에서 연결과 후보 셀 추출 | 구현됨. 단, 입력 파서의 제약이 있음 | `TimingCluster.py` |
| 초기 STA의 sink-pin slack을 이용한 weighted edge table | 구현됨 | `TimingCluster.py` |
| 중복 pin edge를 directed cell edge로 합산 | 구현됨 | `TimingEdgeAggregate.py` |
| topological-order 기반 greedy clustering | 구현됨. timing weight를 선택 점수로 사용하지 않음 | `TimingCluster.py` |
| Leiden community detection | 독립 실행 구현 | `LeidenCluster.py` |
| Leiden 결과의 DAG 및 크기/pin/보수적 arc 예산 보정 | 국소 분할·재검사 구현. 모든 timing 모델 조건의 보장은 아님 | `LeidenCluster.py`, `LeidenGraph.py` |
| 지정 membership에서 reduced Liberty 생성 | 제한된 NLDM 조합논리 모델에 대해 구현됨 | `ReducedLiberty.py` |
| Liberty의 경계 핀과 일치하는 reduced Verilog 생성 | 구현됨 | `ReducedVerilog.py` 및 `ReducedLiberty.py` |
| reduced physical PlaceDB/LEF/DEF로 배치 실행 | **미구현** | 별도 연동 필요 |
| DREAMPlace 반복 루프의 timer를 reduced graph로 자동 교체 | **미구현** | 별도 연동 필요 |
| SDC/SPEF 자동 변환 | **미구현** | 별도 변환·검증 필요 |

현재 clustering flag를 켠다고 기존 placement/timer가 자동으로 축약되지는 않는다. `Placer.py`는 초기 timer를 만든 뒤 clustering 결과를 저장하고, 기존 PlaceDB와 timer로 배치를 계속 수행한다.

## 2. cell, pin, net, driver, sink의 관계

### 2.1 cell과 pin은 다른 객체다

- **Cell instance**: 설계에 배치되는 개별 셀. 예: `u1`, `u2`.
- **Library master**: 셀의 종류. 예: `INV_X1`, `NAND2_X2`.
- **Pin**: 셀의 입출력 단자. 예: `u1/Y`, `u2/A`.
- **Net**: 서로 연결된 단자들의 전기적 연결.
- **Driver**: 해당 net에 신호를 내보내는 출력 pin, 또는 그 pin을 가진 셀.
- **Sink**: 해당 net의 신호를 받는 입력 pin, 또는 그 pin을 가진 셀.

예를 들어:

```text
u1/Y ── net_n ──┬── u2/A
                ├── u3/B
                └── ff0/D
```

`u1/Y`가 driver pin이고 나머지 세 핀이 sink pin이다. `u1`은 driver cell이다. 이 net의 sink-pin fanout은 3이다.

`ff0`를 clustering에서 제외하더라도 원본 net의 fanout은 여전히 3이다. 제외된 연결이 실제 설계에서 없어진 것은 아니다.

### 2.2 driver–sink 관계를 그래프로 만드는 방법

셀 그래프에서는 다음과 같이 표현한다.

```text
u1 → u2
u1 → u3
u1 → ff0     # 원본 연결에는 존재하지만 FF는 community 후보가 아님
```

한 net이 여러 sink를 가지므로, 원래 회로 연결은 hypergraph로 보는 것이 자연스럽다. 현재 edge table은 이를 **driver 중심의 star 형태**로 펼친 것이다.

이 방식은 `u2`와 `u3` 사이에 실제로 존재하지 않는 직접 신호 전달 edge를 만들지 않는다. Sink끼리 모두 연결하는 clique expansion과는 다르다.

### 2.3 directed graph와 undirected graph의 역할

이 프로젝트에서는 두 관점을 분리하는 것이 좋다.

| 그래프 | 사용 목적 | 방향 보존 |
|---|---|---|
| 원본 논리 연결/타이밍 검증용 그래프 | driver/sink, 경계, 경로, cycle, 도달 가능성 검증 | 필수 |
| Leiden용 affinity 그래프 | 함께 묶을 가치가 큰 셀 집합 제안 | 첫 구현은 undirected로 구성 가능 |

Undirected Leiden에서 `u1—u2`는 “두 셀의 결합도를 높게 본다”는 뜻이다. 신호가 실제로 양방향으로 흐른다는 뜻이 아니다.

**Leiden용 그래프를 무방향으로 만들더라도 원본 방향 정보는 반드시 별도로 보존해야 한다.**

### 2.4 cell graph와 실제 pin-level timing graph도 다르다

셀 그래프는 연결을 간략화한 표현이다. 여러 입력·출력을 가진 셀의 모든 입력이 모든 출력에 영향을 주는 것은 아니다. 실제 영향 관계는 Liberty의 `related_pin`, timing sense 및 timing arc로 확인해야 한다.

따라서 cell graph는 후보 탐색과 보수적인 검증에 적합하지만, 최종 reduced timing arc 생성에는 원본 pin-level 연결과 Liberty가 필요하다.

## 3. 반드시 지켜야 하는 제약과 품질 개선용 기준

여기서 “필수”는 두 종류로 구분한다.

1. 연결·타이밍 의미를 잃지 않기 위한 구조적 요구사항.
2. 현재의 단순한 reduced 모델을 안전하게 사용하기 위해 프로젝트가 채택하는 보수적인 정책.

모든 timing abstraction 기법에서 동일한 제한이 수학적으로 필요한 것은 아니다. 더 풍부한 모델을 구현하면 일부 제한은 완화할 수 있다.

### 3.1 구조·안전 정책: 높은 점수로 대체할 수 없는 조건

| 항목 | 요구사항 | 이유 |
|---|---|---|
| Membership | 셀은 최대 한 cluster에만 속함. 미포함 셀은 원본으로 보존 | 중복 배치·중복 타이밍 모델 방지 |
| 조합논리 범위 | FF/latch를 단순 조합논리 cluster 내부에 넣지 않음 | 상태와 clock-to-Q/setup/hold 경계를 보존 |
| Macro/IO/fixed 객체 | 현재 정책에서는 cluster 밖의 anchor로 유지 | 물리 제약과 별도 timing 모델 보존 |
| Clock tree | 데이터 경로 community에서 제외 | clock 의미와 고 fanout 구조 보호 |
| Domain | 알려진 안전한 domain/분석 영역 안에서만 묶음 | CDC·미해석 clock 조건의 임의 혼합 방지 |
| 연결 보존 | 외부와 연결되는 net/pin을 빠짐없이 유지 | 외부 sink, FF, macro, PI/PO 연결 손실 방지 |
| 방향·driver | driver/sink를 식별할 수 있어야 함 | undriven/multi-driver/inout을 일반 단일-driver net처럼 처리하지 않음 |
| Cycle/partition legality | 조합논리 cycle과 축약 후 부적절한 cycle 검사 | 원본에 없던 순환 의존 관계를 만들지 않음 |
| Timing 모델 일치 | reduced Verilog의 master/pin과 reduced Liberty가 일치 | 다른 핀의 delay LUT를 잘못 참조하지 않음 |
| 분석 조건 | cluster 내부 셀의 corner·단위·slew 기준이 호환됨 | 서로 다른 조건의 지연을 무분별하게 합치지 않음 |

위반한 cluster는 점수가 높더라도 그대로 사용하지 않는다. 나누거나, 해당 셀을 원본 인스턴스로 남기거나, 지원 가능한 모델을 추가해야 한다.

### 3.2 예산 제약: 최종 결과에서 재검사할 제한

기본 설정은 다음과 같다.

```text
cluster cell 수                     ≤ 32
boundary input 수 + output 수       ≤ 24
boundary input 수 × output 수       ≤ 64
```

이는 공정의 고정 규칙이 아니라 계산 비용을 제어하기 위한 실험 설정이다. Leiden의 점수나 resolution만으로 이 제한이 보장되지는 않는다.

**현재 greedy 구현에도 예외가 있다.** `_fits()`는 기존 cluster에 셀을 추가할 때만 호출된다. 새 singleton cluster를 만들 때는 boundary/arc 제한을 검사하지 않는다. 따라서 큰 단일 셀 자체가 제한을 넘는 경우에도 cluster로 저장될 수 있다. 최종 검증기가 별도로 필요하다.

### 3.3 Soft objective: 더 좋은 cluster를 선택하는 기준

- 큰 timing weight를 가진 연결을 가능한 한 cluster 내부에 둔다.
- 외부로 잘리는 연결의 총 weight를 줄인다.
- boundary pin 및 실제 reachable timing arc 수를 줄인다.
- 지나치게 큰 cluster와 편중된 크기 분포를 피한다.
- 추후 좌표가 신뢰할 만해지면 면적·형상·물리적 거리도 반영한다.
- 충분한 축약률을 얻되, 중요한 경로의 지연 오차와 배치 자유도 손실을 제한한다.

Soft objective는 “어느 합법적인 해가 더 나은가”를 정한다. Hard constraint를 어기는 해를 정당화하지 않는다.

## 4. clock domain과 sequential boundary

### 4.1 FF 사이의 조합논리 영역

```text
FF_A/Q → U1 → U2 → U3 → FF_B/D
```

현재 조합논리 abstraction에서는 `U1/U2/U3`를 묶을 수 있지만 `FF_A`와 `FF_B`는 원본으로 남겨야 한다. FF의 D→Q 동작을 단순한 combinational edge로 만들어서는 안 된다.

“Sequential cluster”라는 독립 데이터 구조가 현재 코드에 있는 것은 아니다. FF/latch는 **경계를 형성하는 anchor**이고, 그 사이의 후보 셀들을 그래프로 다룬다.

또한 “FF를 후보에서 제외했다”와 “특정 FF의 앞뒤 조합논리가 절대로 같은 community에 들어가지 않는다”는 동일한 명제가 아니다. 우회 경로와 reconvergence가 있으면 후보 그래프가 다른 길로 연결될 수 있다. 후자의 더 엄격한 정책이 필요하면 launch/capture FF 집합 등 anchor provenance에 대한 추가 검증이 필요하다. 현재 코드가 이를 완전히 보장하지는 않는다.

### 4.2 Launch domain과 capture domain

- **Launch domain**: 이 셀까지 신호를 전달할 수 있는 FF들의 clock domain 집합.
- **Capture domain**: 이 셀에서 도달할 수 있는 FF들의 clock domain 집합.

예:

```text
FF(clk_A)/Q → U1 → U2 → FF(clk_A)/D
```

이 경우 launch와 capture가 모두 `clk_A`라면 일반적인 동일-domain 후보로 취급할 수 있다.

```text
FF(clk_A)/Q → U1 → U2 → FF(clk_B)/D
```

이 경우 `U1`, `U2`가 동일한 `(launch=A, capture=B)` 쌍을 갖더라도 알려진 domain crossing이다. 현재 edge exporter는 제외한다.

### 4.3 현재 bitmask 표현

`_derive_clock_domains()`는 다음을 사용한다.

```text
self.launch_domains[node] : FF 출력 쪽에서 순방향 전파한 mask
self.domains[node]        : FF 입력 쪽에서 역방향 전파한 capture mask
```

Clock source port를 정렬하고 각 port에 bit 하나를 부여한다. 일반 clock port는 최대 63개이며, bit 63은 unknown/neutral 표시에 사용한다.

예를 들어 두 clock이 각각 bit 0, bit 1이면:

```text
0001 : 첫 번째 clock
0010 : 두 번째 clock
0011 : 두 clock 모두 관련된 multi-domain logic
```

여기서 “관련”은 현재 코드의 보수적인 연결 전파 결과이다. 완전한 SDC timing scenario 해석 결과가 아니다.

### 4.4 edge table의 현재 domain 필터

Driver `u`, sink `v`에 대해 모두 만족해야 한다.

```text
launch[u] == launch[v]
capture[u] == capture[v]
capture는 알려진 단일 domain
launch는 unknown이 아니며 multi-domain이 아님
launch가 0이 아니면 launch == capture
```

`launch == 0`은 “추적된 FF source가 없다”는 뜻이다. 예를 들어 PI에서 시작한 cone이 이에 해당할 수 있다. **PI의 input-delay clock을 올바르게 해석했다는 뜻은 아니다.**

Clock 정보가 전혀 없으면 edge exporter는 안전한 domain을 확정할 수 없어서 edge를 내보내지 않는다. 반면 greedy용 capture mask에는 이 경우 임시값 1을 넣는다. 두 경로의 정책은 다르다.

### 4.5 현재 greedy는 같은 수준으로 검사하지 않는다

`cluster()`의 병합 조건은 capture mask 값의 동일성만 비교한다. Launch mask와 단일-known-domain 조건을 다시 검사하지 않는다.

따라서 다음을 혼동하면 안 된다.

```text
보수적인 domain 필터를 거친 timing_edges.csv
≠ 그 edge만 이용해 만들어진 현재 greedy cluster
```

Greedy는 PlaceDB 연결을 직접 사용한다. 같은 unknown/multi-domain capture mask를 가진 후보끼리 묶일 가능성도 있다.

### 4.6 파서가 지원하지 않는 clock 의미

현재 SDC 파서는 단순한 `create_clock ... [get_ports ...]` 형태를 읽는다. 다음은 일반적으로 추가 구현·검증이 필요하다.

- Generated clock, 복잡한 Tcl/변수/객체 질의.
- Clock mux, clock gating의 정확한 의미와 mode별 활성 경로.
- PI/PO input/output delay의 clock domain 지정.
- False path, multicycle path, asynchronous clock group의 clustering 의미.
- Latch의 enable 및 time borrowing, 복잡한 sequential Liberty 표현.

Clock port에서 시작한 조합논리 전파로 clock-tree 셀을 표시하는 방식도 보수적인 근사이다. 실제 기능을 해석하는 CTS/SDC 엔진은 아니다.

## 5. Topological order와 현재 greedy 알고리즘

### 5.1 Topological order란?

Directed acyclic graph에서 모든 edge `u → v`에 대해 `u`가 `v`보다 앞에 오도록 나열한 순서이다.

```text
A → B → D
└→ C ──┘

가능한 순서: [A, B, C, D]
다른 순서:  [A, C, B, D]
```

반환되는 `order`는 **1차원 Python list**이다. 그러나 입력 회로가 1차원 chain이라는 뜻은 아니다. 순서는 일반적으로 유일하지 않다.

현재 `_topological_order()`는 특정 FF 하나나 미리 만들어진 cluster 하나가 아니라, **전체 후보 셀의 induced directed graph**에 대해 실행한다. 서로 연결되지 않은 component도 하나의 list에 들어갈 수 있다.

### 5.2 실제 계산: `_topological_order()`

1. `self.candidate`가 참인 셀들을 선택한다.
2. 각 후보 셀의 후보 predecessor 수를 indegree로 계산한다.
3. Indegree가 0인 셀을 ready list에 넣는다.
4. `pop()`으로 하나 꺼내 `order`에 추가한다.
5. 그 셀의 successor indegree를 감소시킨다.
6. 새로 0이 된 successor를 ready list에 넣는다.

Kahn 방식이며 ready list를 LIFO로 사용한다. 연결된 cone을 비교적 연속해서 방문하도록 유도하지만, 최적의 clustering 순서를 찾는 알고리즘은 아니다.

현재 구현의 `residual`은 처리되지 않은 후보들이다. **Cycle 자체의 노드뿐 아니라 cycle 때문에 indegree가 풀리지 않은 downstream 노드도 포함할 수 있다.** 따라서 별도의 `detect_cyclic_cells()`가 SCC 분석으로 실제 순환 멤버만 구분한다.

### 5.3 `cluster()`의 실제 선택 규칙

`order`의 다음 셀에 대해 **마지막으로 만든 cluster 하나만** 후보로 검사한다.

```text
직전 cluster에 predecessor가 존재하는가?
capture-domain mask가 같은가?
_fits()의 셀/경계/arc 예산을 만족하는가?
```

모두 참이면 그 cluster에 추가한다. 아니면 새 singleton cluster를 만든다. 이전의 다른 cluster로 돌아가거나, 여러 cluster 점수를 비교하지 않는다.

`residual` 중 실제 순환 멤버와 critical 보호 셀은 병합에서 제외하고 assignment=-1로 남긴다. 나머지 downstream 노드들은 각각 singleton으로 만든다. 이는 원본 cycle을 삭제하거나 정상적인 reduced Liberty를 보장하는 처리는 아니다.

### 5.4 왜 연속 구간을 사용하는가?

하나의 유효한 topological order에서 cluster가 연속 구간을 차지하면, 그 DAG에서 경로가 cluster를 나갔다가 다시 들어올 수 없다. 경로를 따라 순서가 계속 증가하기 때문이다.

이것은 **topological convexity를 얻는 충분조건**이며, 모든 convex cluster가 반드시 한 임의의 topological order에서 연속인 것은 아니다.

또한 모든 cluster가 **같은** order의 서로 겹치지 않는 구간이면, 그 DAG를 cluster 단위로 축약해도 edge가 구간 순서를 역행하지 않는다.

주의: 현재 이 보장은 필터링된 후보 그래프에 대한 것이다. 제외한 macro/clock cell을 경유하는 경로까지 포함한 완전한 pin-level timing graph의 검증을 대신하지 않는다.

### 5.5 Greedy와 Leiden의 비교

| 항목 | 현재 topological greedy | 제안하는 Leiden 기반 흐름 |
|---|---|---|
| 입력 관점 | 방향 있는 후보 연결과 순서 | weighted affinity와 원본 directed graph |
| Timing weight 사용 | clustering 선택에 사용하지 않음 | community 제안 점수에 사용 |
| 탐색 범위 | 마지막 cluster에만 추가 | 여러 이웃 community 사이 재배치 가능 |
| 순서 의존성 | 큼 | random seed/목적함수/해상도 등의 영향 |
| Convexity 확보 | 동일 order 연속 구간이라는 보수적 규칙 | 별도 legalization 필요 |
| Boundary/arc 예산 | 병합 시 `_fits()` 적용 | 제안 후 별도 검사·분할 필요 |
| 장점 | 구조가 단순하고 설명하기 쉬움 | 결합도가 큰 부분구조를 더 유연하게 찾을 수 있음 |
| 한계 | 분기·합류 관계가 순서 때문에 잘릴 수 있음 | community 품질이 회로 legality를 보장하지 않음 |

따라서 “Leiden이 무조건 더 좋다”가 아니라, **추가 검증 비용을 감수하고 더 좋은 후보를 탐색할 가치가 있는가**를 실험해야 한다.

## 6. 초기 STA, slack, fanout 및 timing weight

### 6.1 언제 OpenTimer를 호출하는가?

Standalone `run()`은 timer를 만들고 `update_timing()`을 한 번 호출한다. DREAMPlace 연동에서는 `Placer.py`가 만든 초기 timer를 재사용한다.

그 다음 `extract_critical_cells()`로 보호 셀을 저장하고, `build_timing_edgelist()`가 보호 셀에 연결된 edge를 제외하면서 원본 net들을 순회해 각 유효 sink pin의 slack을 조회한다. **각 edge마다 OpenTimer 전체를 다시 초기화하거나 STA를 반복하는 구조는 아니다.**

이 단계는 아직 placement 위치 기반 RC construction을 실행하지 않는다. Liberty의 cell delay, pin load 및 읽어들인 제약에 따른 초기 타이밍 정보이며, 배치 후 wire delay를 반영한 최종 criticality가 아니다.

배치 중 RC 업데이트는 `ops/timing/timing.py`의 `TimingOptFunction.forward()` 등 별도의 경로이다.

### 6.2 `timing_slack_ps`의 정확한 뜻

현재 조회는 다음에 해당한다.

```python
timer.raw_timer.report_slack(sink_pin_name, True, transition)
```

Rise/fall의 **MAX, 즉 setup 쪽 sink-pin slack** 중 유한한 값의 최솟값을 사용한다. Timer의 시간 단위를 ps로 환산한다.

이 값은 다음과 다르다.

- Driver pin과 sink pin 사이의 독립적인 edge slack.
- `sink_slack - driver_slack`.
- 해당 net의 delay.
- Cluster 내부 입력→출력 delay.
- Hold 쪽 early/min slack.

Setup slack의 직관적인 정의는 `required arrival time - actual arrival time`이다. 현재 값은 그 sink에 도달하는 경로와 downstream 요구 조건의 영향을 받는 **pin-level 중요도 proxy**이다.

### 6.3 현재 계산식

기호를 다음과 같이 놓자.

- `s`: worst finite sink slack, ps.
- `f`: 원본 net의 모든 INPUT sink pin 수.
- `tau`: slack 감소 척도, 기본 100 ps.
- `alpha`: timing 중요도 증폭 계수, 기본 4.

```text
fanout_weight = 1 / sqrt(max(1, f))
criticality   = exp(-max(s, 0) / tau)
timing_weight = 1 + alpha × criticality²
edge_weight   = fanout_weight × timing_weight
```

보호 셀에 연결된 edge를 제외한 나머지 edge에서 작은 slack일수록 criticality가 1에 가까워져 두 셀을 함께 묶는 affinity를 높인다. 즉 hard protection과 남은 edge의 soft timing weight는 별개다. 이 설계는 중요한 연결을 자르는 것을 줄이려는 휴리스틱이지, clustering이 WNS/TNS를 개선한다는 보장은 아니다.

### 6.4 수치 예제

`fanout=4`, `tau=100 ps`, `alpha=4`이면:

| Sink slack | Criticality | Timing weight | 최종 edge weight |
|---:|---:|---:|---:|
| -50 ps | 1.0000 | 5.0000 | 2.5000 |
| 0 ps | 1.0000 | 5.0000 | 2.5000 |
| 100 ps | 0.3679 | 1.5413 | 0.7707 |
| 300 ps | 0.0498 | 1.0099 | 0.5050 |

현재 식은 음수 slack을 모두 같은 최대 중요도로 취급한다. `-10 ps`와 `-1000 ps`의 심각도를 추가로 구별하지 않는다. 필요하면 clipping한 violation 항 등을 추가할 수 있지만 **현재 구현된 식은 아니다**.

### 6.5 Fanout weight가 하는 일과 하지 않는 일

큰 fanout의 각 edge를 약하게 만들어, 넓게 퍼진 net 하나가 clustering을 지나치게 지배하는 것을 완화한다.

단, `1/sqrt(f)`는 net 전체 weight를 일정하게 만드는 정규화가 아니다. 모든 sink의 timing weight가 같으면 총 fanout weight는 약 `sqrt(f)`에 비례한다. 고 fanout net의 전체 영향은 여전히 커질 수 있다.

`1/f` 정규화나 reset/scan-enable 등 특수 net 처리도 실험할 수 있지만, 이는 현재와 다른 정책이다. Affinity에서 edge를 줄이더라도 **경계·legality 검사에서는 원본 연결을 삭제하면 안 된다.**

### 6.6 누락된 slack

- Rise/fall 모두 finite: `slack_status=valid`.
- 하나만 finite: 그 값을 사용하고 `partial`.
- 둘 다 nonfinite: slack 칸은 비우고 `missing`.

Missing이면 `criticality=0`, `timing_weight=1`로 둔다. 이것은 “안전한 경로” 판정이 아니라 timing 근거가 없어 topology/fanout만 반영하는 fallback이다. Missing 비율이 높으면 SDC, pin 이름 매칭, 모델 및 unconstrained path부터 조사해야 한다.

### 6.7 배치 전 physical weight

현재 식에는 좌표 기반 weight가 없다. 초기 좌표가 의미 없으면 거리 weight를 억지로 넣지 않는 것이 적절하다.

다만 배치 전에도 셀 면적, site, 허용 region, macro/IO 위치 같은 정적 물리 정보는 알 수 있다. 이는 좌표 거리와 별개로 cluster 예산이나 금지 조건에 사용할 수 있다. 현재 greedy에는 area/shape 예산이 구현되어 있지 않다.

## 7. Pin edge를 cell edge로 합치는 방법

### 7.1 `timing_edges.csv`는 pin 연결 단위다

예를 들어 같은 두 셀이 여러 입력 핀으로 연결되면:

```text
driver_id  driver_pin  sink_id  sink_pin  weight
10         u10/Y       20       u20/A     1.5
10         u10/Y       20       u20/B     1.0
```

두 행은 다른 pin 연결이다. Driver/sink ID가 같다는 이유만으로 잘못 중복 생성되었다고 볼 수 없다.

### 7.2 Leiden의 vertex는 cell instance로 둔다

`TimingEdgeAggregate.py`는 ordered pair `(driver_id, sink_id)`별로 합친다.

```text
w_cell(u,v) = Σ pin-edge weight(u → v)
```

위 예제의 결과는 `10 20 2.5`이다.

Clustering affinity에서는 기본적으로 `sum`을 사용한다. 두 셀 사이 여러 연결의 총 결합도를 보존하기 때문이다. `max`는 가장 강한 한 연결만 남겨 multiplicity를 버리는 다른 목적함수이다.

**합산하는 것은 weight이지 slack이나 delay가 아니다.** 다른 경로의 slack들을 더해 cell slack으로 만드는 것은 여기서 하지 않는다.

### 7.3 방향을 합칠 때

Aggregator는 `u→v`와 `v→u`를 서로 다른 행으로 보존한다. Undirected affinity를 만들 때만 다음처럼 합칠 수 있다.

```text
w_affinity({u,v}) = w_cell(u,v) + w_cell(v,u)
```

방향 있는 원본은 별도로 보존한다. 양방향 edge가 있다는 사실을 무방향화로 숨기지 말고 원본 그래프의 cycle 여부를 확인해야 한다.

### 7.4 ID와 누락된 vertex

출력 ID는 해당 PlaceDB에 속한 숫자 ID다. `0...N-1`로 연속되어 있다고 가정하면 안 된다. Leiden/igraph에는 별도의 dense vertex index를 만들고 원본 ID 역매핑을 저장해야 한다.

Aggregator의 `cell_names.tsv`는 **입력 edge에 등장한 ID의 합집합만** 포함한다. 다음은 빠질 수 있다.

- 필터로 모든 edge가 제외된 후보 셀.
- 연결이 없는 singleton 후보.
- FF/macro/IO 등 anchor.

따라서 edge endpoint 목록만으로 전체 설계가 partition되었다고 판단하면 안 된다. 누락된 셀은 원본으로 남기거나, 별도의 전체 후보 목록을 통해 singleton 처리해야 한다.

완전한 Leiden 입력에는 향후 다음과 같은 vertex metadata가 필요하다. **이 파일은 현재 자동 생성되지 않는다.**

```text
cell_id  cell_name  eligible  launch_mask  capture_mask  exclusion_reason
```

## 8. Leiden을 어디에, 어떻게 사용하는가?

### 8.1 Leiden의 역할은 community 후보 생성이다

Leiden은 local moving, refinement, aggregation을 반복하는 community detection 알고리즘이다. Louvain의 연결성 문제를 개선하지만, 해당 그래프에서의 community 연결성은 clock 경계나 timing legality 보장과 다르다. [Leiden 원 논문](https://www.nature.com/articles/s41598-019-41695-z)

이 프로젝트에서의 역할은 다음과 같다.

```text
원본 netlist / Liberty / SDC / PlaceDB
                 │
        초기 STA 및 안전한 edge 추출             [현재 구현]
                 │
        pin edge → cell edge 합산                [현재 구현]
                 │
        domain별 weighted Leiden 후보 생성      [미구현]
                 │
        원본 directed graph로 검사·분할          [미구현]
                 │
        최종 cell_id → cluster_id
                 │
        reduced Liberty + reduced Verilog       [제한된 범위에서 구현]
                 │
        reduced STA / coarse physical placement [STA 입력 가능, 배치 연동은 별도]
```

### 8.2 여러 개의 network가 있어도 되는가?

된다. 모든 셀이 하나의 connected component에 속할 필요는 없다. FF/macro/clock tree를 제외하면 여러 weakly connected component가 생기는 것이 자연스럽다.

권장 구조는 **hard domain bucket별로 나누고, 그 안의 component별로 Leiden을 실행**하는 것이다. 이렇게 하면 다른 domain을 섞지 않는 조건을 weight에 맡기지 않아도 된다.

Directed acyclic graph의 strongly connected component는 보통 singleton이다. 따라서 SCC를 community 자체로 사용하면 거의 축약되지 않는다. SCC는 cycle 탐지용이고, 여기서 말하는 component 분리는 대개 **방향을 무시한 weak connectivity** 기준이다.

### 8.3 목적함수와 resolution

첫 실험의 후보로 weighted CPM을 사용할 수 있다. 무방향 그래프에서 직관적으로는 다음 꼴이다.

```text
Q = Σ_cluster [ 내부 edge weight 합 - gamma × n × (n-1)/2 ]
```

`gamma`를 높이면 큰 community의 비용이 커지는 방향으로 작용한다. `gamma`는 weight 크기와 함께 해석해야 하며, 다른 정규화 방식에서 같은 숫자가 같은 크기의 cluster를 만든다고 가정하면 안 된다. CPM과 `resolution_parameter`는 `leidenalg`가 제공하는 선택지이다. [공식 소개](https://leidenalg.readthedocs.io/en/stable/intro.html)

이 프로젝트에서는 resolution을 cell 수 제한의 대체재로 사용하지 않는다. 여러 값을 시험한 뒤 **legalization 이후**의 축약률과 timing 오차로 비교한다.

### 8.4 API 사용 예시 — 현재 코드에 통합된 기능은 아님

다음은 이미 domain/component가 분리되고 edge가 무방향으로 합산된 입력에 대한 **후보 생성 예시**이다. 자체적으로 timing-safe clustering을 완성하지 않는다.

```python
import igraph as ig
import leidenalg as la

# cell_ids: 이 bucket의 모든 대상 ID. edge가 없는 vertex도 필요하면 포함.
# edges: (원본 cell_id_u, 원본 cell_id_v, 양의 affinity weight)
# gamma: 실험자가 정한 resolution. 검증된 범용 기본값이 아님.
ids = sorted(cell_ids)
local = {cell_id: i for i, cell_id in enumerate(ids)}
g = ig.Graph(
    n=len(ids),
    edges=[(local[u], local[v]) for u, v, weight in edges],
    directed=False,
)
g.es['weight'] = [weight for u, v, weight in edges]
partition = la.find_partition(
    g,
    la.CPMVertexPartition,
    weights='weight',
    resolution_parameter=gamma,
    max_comm_size=32,
    seed=42,
    n_iterations=2,
)
candidate_clusters = [[ids[i] for i in community] for community in partition]
# 여기서 종료하면 안 됨: 원본 directed graph에서 legality 검사·분할 필요.
```

`find_partition`은 weight, seed, 반복 횟수와 최대 community size를 받을 수 있다. 위 예시는 vertex size가 기본 1인 경우다. Area를 `node_sizes`로 사용하면 size 제한의 의미도 바뀌므로 별도의 cell 수 검사가 필요하다. [공식 API](https://leidenalg.readthedocs.io/en/stable/reference.html#leidenalg.find_partition)

대규모 설계에서는 이 Python list 예제를 그대로 복제하기보다 compact edge arrays와 재사용 가능한 index를 사용해야 한다. Seed뿐 아니라 vertex/edge 정렬, 라이브러리 버전, 입력 snapshot도 기록해야 비교가 가능하다.

Directed Leiden을 사용할 수도 있지만, directed objective를 택했다고 FF 경계·convexity·boundary 예산이 자동으로 해결되지는 않는다. 첫 구현에서 affinity와 timing legality를 분리하는 이유다.

## 9. Leiden 결과의 legality 검사와 분할

### 9.1 커뮤니티를 그대로 cluster로 확정하면 안 되는 이유

다음 원본 DAG를 보자.

```text
A → B → C
```

후보가 `{A,C}`와 `{B}`이면, cell-level 축약은 다음처럼 된다.

```text
Cluster_AC → Cluster_B → Cluster_AC
```

원본은 DAG지만 축약 그래프에는 cycle이 나타난다. Affinity 점수가 좋더라도 이 후보는 보수적인 coarse graph 정책상 사용할 수 없다.

### 9.2 Topological convexity

Cluster 내부 두 노드를 잇는 directed path가 cluster 밖으로 나갔다가 다시 들어오지 않도록 요구하는 정책이다. 위 `{A,C}`는 이 조건을 위반한다.

검사는 **weight filtering 이전의 원본 연결**에 대해 해야 한다. 낮은 weight나 큰 fanout 때문에 Leiden 입력에서 삭제한 edge도 실제 timing/netlist에는 남아 있기 때문이다.

또한 개별 cluster의 convexity만 확인하고 전체 quotient graph 검사를 생략해서는 안 된다. 예를 들어:

```text
A1 → A2       B1 → B2
A1 → B2       B1 → A2

Cluster_A = {A1,A2}, Cluster_B = {B1,B2}
```

각 집합에 나갔다가 재진입하는 경로가 없어도, cell 단위 quotient에는 `A↔B`가 생긴다. 이는 coarse cell graph가 pin별 의존 관계를 합쳐 보는 데 따른 보수성도 포함한다. 최종 pin-level timing 모델과 cell-level 안전 정책의 차이를 인식해야 한다.

### 9.3 제안하는 초기 legalization

복잡한 최적화보다 먼저 다음과 같은 보수적인 절차를 사용할 수 있다. **아직 구현된 함수는 아니다.**

1. 금지된 셀/domain이 섞인 후보를 분리한다.
2. 원본 연결을 기준으로 disconnected 부분을 나눈다.
3. 안전하게 정의한 조합논리 검증 그래프에서 공통 topological order를 구한다.
4. 각 Leiden label을 그 order의 연속 run으로 나눈다.
5. 각 run을 predecessor-connected하게 성장시키되 cell/boundary/arc 예산을 검사한다.
6. 모든 cluster에 대해 정확한 외부 경계와 실제 reachable arc를 재계산한다.
7. 전체 축약 그래프와 retained-cell 경유 경로를 검증한다.
8. ReducedLiberty의 모델 제한을 위반하는 cluster는 더 나누거나 원본으로 남긴다.

이 방법은 안전한 baseline이지만, 순서가 나쁘면 Leiden community를 많이 쪼개어 장점을 잃을 수 있다. 이후에는 community 내부의 국소 재배치·분할을 사용하되 **동일한 최종 검증기**를 통과시키는 방식으로 개선할 수 있다.

현재 `_topological_order()` 결과를 무조건 전체 회로 검증 그래프로 취급해서는 안 된다. 제외한 조합논리 블록을 경유하는 경로, FF cut의 의미, multi-output pin 의존 관계를 검증 범위에 맞게 처리해야 한다.

### 9.4 실패 시 정책

- 잘 나눌 수 있으면 subdivision을 반복한다.
- 단일 셀도 boundary 예산이나 모델 제한을 만족하지 않으면 **원본 인스턴스로 유지**한다.
- Unknown clock/CDC/unsupported mode는 임의 추정으로 묶지 않는다.
- Cycle residual을 singleton으로 만들었다고 문제가 해결되었다고 기록하지 않는다.
- 전역 실패율, 실패 이유, 원본으로 남긴 셀 수를 저장한다.

현재 `ReducedLiberty.py`는 지원하지 않는 cluster에서 오류를 내며, 이 자동 분할/fallback 정책을 대신 구현해 주지는 않는다.

## 10. Boundary pin과 reduced timing arc

### 10.1 Boundary input/output

Cluster `C`에 대해:

```text
Input boundary net:
    driver는 C 밖에 있고, sink 중 적어도 하나가 C 안에 있음

Output boundary net:
    driver는 C 안에 있고, sink 중 적어도 하나가 C 밖에 있음
```

여기서 “밖”에는 다른 cluster뿐 아니라 PI/PO, FF, macro, IO와 unclustered cell도 포함한다.

현재 추상 pin은 **외부와 연결되는 고유 net별**로 만든다. 한 input net이 내부 입력 핀 10개에 연결되어도 boundary input은 하나가 될 수 있으며, input capacitance에는 내부 sink들의 부하가 반영되어야 한다.

Physical macro의 access point 개수와 이 논리적 boundary pin 개수는 별개다. 실제 coarse placement에서는 abstract pin의 위치·offset 정책도 추가로 필요하다.

### 10.2 왜 pin 수와 arc 수를 제한하는가?

입력 수 `I`, 출력 수 `O`라면 가능한 입력→출력 조합의 상한은 `I×O`다. 실제 arc는 원본 타이밍 경로가 도달 가능한 쌍에 대해서만 필요하다.

현재 greedy `_fits()`와 `clusters.csv`의 `timing_arcs`는 실제 arc enumeration이 아니라 **`I×O`라는 보수적 상한**을 사용한다. Liberty LUT가 생성되었다는 뜻이 아니다.

Reduced Liberty는 각 reachable arc에 rise/fall delay와 output slew table을 생성한다. 셀 수를 줄여도 boundary와 arc가 폭증하면 모델 생성·STA 비용이 커진다.

### 10.3 Slack은 delay LUT가 아니다

Clustering 단계의 slack은 “어떤 연결을 중요하게 볼 것인가”에 사용한다.

Characterization 단계에서는 원본 셀 모델과 연결을 사용해 다음을 계산한다.

```text
delay = f(input slew, output load)
output slew = g(input slew, output load)
```

여러 slew/load 점에서 계산한 값을 reduced Liberty에 저장한다. `timing_edges.csv`의 slack만으로 이 테이블을 복원할 수 없다.

### 10.4 현재 ReducedLiberty가 추가로 요구하는 조건

현재 구현은 다음 범위로 제한된다.

- Unconditional unate combinational NLDM arc.
- 내부 조합논리 DAG.
- 지원되는 scalar pin 연결과 flat structural Verilog.
- 같은 I/O 쌍에 대한 mixed-polarity reconvergence가 없음.
- Boundary output이 내부 경로를 통해 다른 boundary output을 구동하지 않음.
- 단일 driver이며 inout/tristate를 일반 조합논리 연결로 다루지 않음.

마지막에서 두 번째 조건은 **여러 output load 사이의 결합**을 독립적인 2D table로 모델링하지 않기 때문이다. 일반적인 모든 timing abstraction의 한계가 아니라 현재 characterization 방식의 제한이다.

따라서 “Leiden의 connected community이고 크기 제한을 만족한다”만으로 reduced Liberty 생성이 가능한 것은 아니다.

## 11. Reduced Liberty·Verilog·OpenTimer의 연결

예를 들어 원본이 다음과 같다고 하자.

```verilog
INV_X1 u1 (.A(a), .Y(n));
INV_X1 u2 (.A(n), .Y(y));
```

두 셀을 cluster 7로 characterization하면, 실제 pin mapping에 따라 reduced netlist는 다음처럼 된다.

```verilog
TC_7 __tc_cluster_7 (.I0(a), .O0(y));
```

OpenTimer에는 다음이 함께 필요하다.

1. `TC_7`의 timing arc를 정의한 reduced Liberty.
2. `TC_7`를 인스턴스로 사용하는 `reduced.v`.
3. 남겨진 FF/macro/IO/일반 셀의 원본 Liberty.
4. Reduced view와 일치하는 timing constraints.
5. 배선 지연까지 평가하려면 reduced view에 맞는 RC 정보.

현재 `ReducedLiberty.py`는 successful characterization 후 `reduced.v`를 자동 생성한다. 이미 characterization한 결과로 Verilog만 만들 때는 `ReducedVerilog.py`를 실행한다.

### 11.1 원본 timing view를 그대로 유지하면 속도 이점이 없는가?

배치 객체만 줄이면 placement 쪽은 빨라질 수 있지만, full timing graph에 대한 RC/STA 비용은 남는다. 타이밍까지 줄이려면 실제 timer에 reduced Verilog/Liberty를 읽혀야 한다.

전체 효과는 대략 다음 비용을 모두 포함해서 측정해야 한다.

```text
초기 full STA + graph 생성 + clustering + characterization
+ 반복 횟수 × (coarse placement + boundary RC + reduced STA)
+ 최종 refinement 및 full-design 검증
```

Initial full STA를 한 번 사용하는 것과 full STA를 모든 후보/iteration에서 반복하는 것은 다르다. Characterization 비용이 크면 재사용 횟수가 적을 때는 오히려 느려질 수 있다.

### 11.2 Timing accuracy에 대한 주의

현재 reduced 모델의 내부 wire는 ideal이다. Critical edge를 cluster 내부에 넣으면 외부 cut은 줄지만, 실제 내부 wire delay가 모델에서 빠지는 오차가 생길 수 있다.

따라서 큰 critical cluster가 항상 더 좋다는 결론은 성립하지 않는다. 중요한 경로일수록 cluster 크기 제한, 내부 RC 모델, full-design 재검증이 더 중요할 수 있다.

기존 SPEF와 내부 셀을 참조하는 SDC를 그대로 적용하지 않는다. Reduced cell pin mapping, boundary RC 재구성 및 constraint remapping이 필요하다. 현재는 자동화되어 있지 않다.

자세한 생성기 사용법은 [REDUCED_LIBERTY.md](REDUCED_LIBERTY.md)를 참고한다.

## 12. 현재 코드의 함수·데이터·파일·실행 방법

### 12.1 주요 데이터

| 변수 | 저장하는 내용 |
|---|---|
| `self.nphys` | 실제 physical node 수. Movable, terminals, terminal_NIs 포함, filler 제외 |
| `self.nmove` | Movable node 수 |
| `db.pin2node_map[p]` | Pin ID `p`가 속한 cell/node ID |
| `db.pin2net_map[p]` | Pin ID `p`가 연결된 net ID |
| `db.pin_direct[p]` | 해당 pin의 방향. 코드에서는 `b"INPUT"`, `b"OUTPUT"`와 비교 |
| `db.node2pin_map[n]` | Node `n`에 속한 pin ID들 |
| `db.net2pin_map[e]` | Net `e`에 속한 pin ID들 |
| `self.driver[e]` | Net `e`의 driver **node ID**. Driver pin ID가 아님 |
| `self.candidate[n]` | Node `n`이 구조적 조합논리 후보인가 (보호 셀은 topology 보존을 위해 여기에는 유지) |
| `self.critical_nodes` | 실제 선택 경로의 셀 ID 집합; edge와 병합 후보에서 제외 |
| `self.cyclic_nodes` | 구조적 SCC의 순환 셀 ID 집합; edge와 병합 후보에서 제외 |
| `self.assignment[n]` | 배정된 cluster ID, 미배정은 -1 |
| `self.domains[n]` | Capture-domain bitmask |
| `self.launch_domains[n]` | Launch-domain bitmask |

Top-level Verilog input은 회로 안으로 신호를 공급하는 source이지만, top-level `input`이라는 문법과 PlaceDB의 pin 방향 표현을 단순히 같은 의미라고 가정해서는 안 된다. 코드의 연결 판단은 로더가 구성한 PlaceDB pin direction을 사용한다.

### 12.2 함수별 읽는 순서

| 함수 | 역할 |
|---|---|
| `prepare()` | 파일에서 anchor 종류를 추출하고 candidate/driver/domain 구성 |
| `parse_liberty_boundaries()` | 제한된 Liberty 구조에서 FF/latch master와 clock pin 이름 추출 |
| `parse_lef_blocks()` | BLOCK/PAD와 site 크기 읽기 |
| `scan_anchor_instances()` | 원본 Verilog의 anchor instance를 PlaceDB ID에 연결 |
| `_derive_clock_domains()` | Clock-tree 제외와 launch/capture mask 전파 |
| `_predecessors()` / `_successors()` | PlaceDB 연결에서 중복 제거한 이웃 node 조회 |
| `_topological_order()` | 후보 directed graph의 순서와 residual 반환 |
| `detect_cyclic_cells()` / `extract_cyclic_cells()` | 전체 비순차 physical graph의 SCC 검사와 순환 셀 목록 저장 |
| `build_timing_edgelist()` | 필터·fanout·sink slack을 반영한 pin-edge CSV 생성 |
| `_boundary()` | Cluster의 input/output boundary net 집합 계산 |
| `_fits()` | 기존 cluster에 node를 추가했을 때 예산 검사 |
| `cluster()` | Topological-order 기반 greedy 배정 |
| `save()` | Greedy membership·통계·boundary·DOT 저장 |
| `run()` | 준비 → 초기 timer → edge export → 선택적으로 greedy/save |

### 12.3 출력 파일은 서로 다른 단계의 계약이다

| 파일 | 생성기 | 의미 |
|---|---|---|
| `timing_edges.csv` | TimingCluster | Pin 연결, domain, fanout, slack, weight |
| `timing_edges_summary.json` | TimingCluster | Weight 설정과 누락/제외 통계 |
| `cell_edges.tsv` | Aggregator, 사용자가 지정한 이름 | Directed cell pair와 합산 weight |
| `cell_names.tsv` | Aggregator, `--cell-map`으로 지정 | Edge endpoint에 등장한 ID/name |
| `cluster_map.jsonl` | TimingCluster | Greedy cluster의 **셀 이름 목록**과 boundary net |
| `clusters.csv` | TimingCluster | Cluster 크기, I/O 수, `I×O` 상한 |
| `boundary_pins.csv` | TimingCluster | Greedy abstraction의 pin/net 이름 |
| `cluster_graph.dot` | TimingCluster | 기본 최대 500개 cluster의 일부 그래프 |
| `clusters.txt` | 최종 partition 단계에서 준비해야 함 | 숫자 `cell_id cluster_id` membership |
| `cluster_mapping.jsonl` | ReducedLiberty | Characterization된 master와 정확한 pin/net 연결 |
| `clusters_Early.lib`, `clusters_Late.lib` | ReducedLiberty | 검증된 min/max timing table |
| `reduced.v` | ReducedLiberty/ReducedVerilog | 실제 abstract instance를 사용하는 STA netlist |

특히 **`cluster_map.jsonl`과 `cluster_mapping.jsonl`은 같은 파일 형식이 아니다.** Greedy의 이름은 `TC_%06d`, reduced Liberty의 이름은 `TC_<id>` 형식이므로 임의로 문자열을 맞춰 쓰면 안 된다. Reduced Verilog는 characterization 결과인 후자의 mapping을 사용한다.

현재 greedy membership은 숫자 `cell_id cluster_id` 파일로 바로 출력되지 않는다. 이름→ID 매핑으로 변환하는 단계가 필요하며, edge endpoint만 있는 `cell_names.tsv`에 없는 셀이 없는지도 확인해야 한다. 파일 확장자만 바꿔서 사용할 수 없다.

### 12.4 실행: weighted edge table

기존 superblue1 설정 예시:

```bash
cd /mnt/hdd1/XP_timing_4.1/bin
python3 dreamplace/TimingCluster.py test/iccad2015.ot/superblue1.json \
  --edges-only \
  --output results/superblue1/timing_edges \
  --timing-tau-ps 100 --timing-alpha 4
```

`--edges-only`는 greedy clustering을 실행하지 않는다.

### 12.5 실행: cell-pair 합산

```bash
python3 dreamplace/TimingEdgeAggregate.py \
  results/superblue1/timing_edges/timing_edges.csv \
  results/superblue1/timing_edges/cell_edges.tsv \
  --cell-map results/superblue1/timing_edges/cell_names.tsv \
  --merge sum --delimiter tab
```

출력은 숫자 `(driver_id, sink_id)` 순으로 정렬된다. ID/name 표도 숫자 ID 순으로 정렬된다. 기본 구분자는 tab이며 기존 출력 파일은 덮어쓰지 않는다.

현재 TimingCluster exporter 자체는 comma CSV를 쓴다. `.csv`라는 이름이 delimiter를 보증하는 것은 아니며 Aggregator는 tab/comma 입력을 자동 감지한다.

### 12.6 실행: 현재 greedy baseline

```bash
python3 dreamplace/TimingCluster.py test/iccad2015.ot/superblue1.json \
  --output results/superblue1/timing_clusters \
  --max-cells 32 --max-boundary-pins 24 --max-timing-arcs 64
```

이 명령은 초기 edge table도 저장하지만 **Leiden을 실행하지 않는다.** 현재 `--method leiden` 같은 옵션은 없다. TimingCluster의 출력은 write mode이므로 이전 결과를 보존하려면 새 출력 디렉터리를 사용한다.

### 12.7 최종 membership이 있을 때 reduced 모델 생성

```bash
python3 dreamplace/ReducedLiberty.py \
  --clusters /path/to/clusters.txt \
  --cell-names /path/to/cell_names.tsv \
  --verilog /path/to/original.v \
  --lib-dir /mnt/hdd1/PNR/unosilicon/ddi/LIB/LX_lib_20260915 \
  --output /path/to/new_reduced_result
```

`--lib-dir`는 ReducedLiberty의 옵션이다. **TimingCluster의 옵션이 아니다.** 공통 directory 모드의 Early/Late 출력은 같은 입력 조건에서의 min/max propagation 모델이며 새로운 fast/slow process corner를 만들어내지 않는다.

## 13. 알려진 한계와 다음 구현 순서

### 13.1 현재 구현을 읽을 때 특히 주의할 점

1. **Leiden과 DAG 보정은 별도 `LeidenCluster.py`에 있다.** TimingCluster의 edge 준비 단계와 혼동하지 않는다. 실제 timing arc characterization 제약은 별도 검증한다.
2. **Greedy는 timing weight를 쓰지 않는다.** Timing-aware라는 파일명만으로 criticality 최적화가 된다고 해석하지 않는다.
3. **Greedy와 edge exporter의 domain 조건이 다르다.** Greedy는 capture mask 동일성만 본다.
4. **TimingCluster의 Liberty 입력은 여전히 단일 파일이다.** `prepare()`는 `late_lib_input` 또는 `lib_input` 하나에서 sequential master를 읽는다. 여러 파일에 FF 종류가 분산된 실제 설계에서는 잘못 후보에 포함할 위험이 있다. 최근 추가한 다중 `.lib` directory 처리는 ReducedLiberty에만 구현되어 있다.
5. **간단한 structural parser다.** `ff_bank`, `latch_bank`, 복잡한 Liberty 표현, 여러 줄 Verilog instance header 등에 대해 누락 가능성을 검토해야 한다.
6. **Greedy용 driver 배열에는 single-driver 검증이 없다.** 여러 OUTPUT pin이 같은 net에 있으면 driver 정보가 덮여 하나만 남을 수 있다. Edge exporter는 ambiguous net을 제외하지만 그 검사가 greedy의 원본 배열을 고치지는 않는다.
7. **Singleton 예산 검사가 없다.** Summary의 설정값만 보고 모든 cluster가 예산을 지켰다고 단정하지 않는다.
8. **Residual은 cycle 멤버와 정확히 같지 않다.** SCC 및 downstream 영향 구분이 필요하다.
9. **`summary.json`의 boolean은 독립적인 검증 증명서가 아니다.** 현재 코드가 고정값으로 기록하는 항목이 있으므로 원본 그래프와 결과에 대한 checker가 필요하다.
10. **Filtering으로 빠진 셀의 처리가 별도로 필요하다.** Edge table에 없다고 원본 netlist에서 삭제하면 안 된다.
11. **하나의 초기 MAX slack만 사용한다.** Multi-corner/multi-mode, hold criticality, 배치 후 RC를 포괄하지 않는다.
12. **Physical reduction과 full-loop timer 교체는 별개다.** 파일 생성만으로 전체 계산 속도 향상이 자동 발생하지 않는다.

이 항목들은 문서 작성 중 확인한 현재 코드의 한계이며, 이 문서가 해당 동작을 수정한 것은 아니다.

### 13.2 권장 구현 순서 — 이 프로젝트의 설계 제안

1. TimingCluster/TimingIO까지 multi-library 입력과 sequential master 분류를 확장한다.
2. 전체 vertex metadata와 원본 directed connectivity를 준비하고, unknown/unsupported 객체를 명시한다.
3. Domain bucket 및 component 단위로 weighted Leiden 후보를 만든다.
4. Greedy와 Leiden 양쪽 결과에 공통으로 적용할 final legality checker를 만든다.
5. 실패 cluster의 분할·원본 유지 정책을 구현한다.
6. 숫자 membership을 저장하고 reduced Liberty/Verilog 생성 성공률을 평가한다.
7. Reduced STA를 검증한 뒤 physical cluster 크기·핀 위치·boundary RC를 연결한다.
8. DREAMPlace 반복 루프와 macro 후보 생성 흐름에 연동하고 full-design 결과로 비교한다.

먼저 Leiden만 연결하고 나중에 constraints를 생각하기보다, **최종 검증 계약을 먼저 확정**하는 편이 결과를 비교하기 쉽다.

## 14. 실험 지표와 최종 체크리스트

### 14.1 측정할 지표

- 원본 셀/핀/arc 수 대비 reduced 셀/핀/arc 수.
- Candidate 대비 실제 cluster 포함률과 원본 유지 셀 수.
- Cluster 크기, boundary 수, reachable arc 수의 평균·최대·분포.
- Critical weighted cut 및 전체 weighted cut.
- Unknown clock, missing/partial slack, ambiguous net 비율.
- Leiden 실행 시간, legalization 시간, characterization 시간, 메모리 peak.
- Grid round-trip 및 holdout slew/load에서의 delay/slew 오차.
- 같은 조건의 full/reduced STA에서 endpoint arrival, slack, WNS/TNS 차이.
- 최종 macro 후보의 품질과 full-design refinement 후의 성능.
- 초기 준비 비용을 포함한 end-to-end 시간과 반복당 실제 절감 시간.

모든 cluster를 singleton으로 만들면 많은 구조적 검사를 쉽게 통과할 수 있지만 축약 효과는 거의 없다. 반대로 크기만 키우면 모델 오차와 boundary 비용이 커질 수 있다. **합법성, 정확도, 축약률, 시간**을 함께 비교해야 한다.

### 14.2 실행 전 확인

- [ ] 모든 셀 master가 올바른 원본 Liberty에서 식별되는가?
- [ ] FF/latch/macro/IO/fixed/clock-tree 분류가 검증되었는가?
- [ ] Clock 및 PI/PO domain 정보의 미지원 부분이 드러나 있는가?
- [ ] 원본 driver–sink 방향과 ambiguous net을 확인했는가?
- [ ] Cell ID/name 표가 같은 netlist/PlaceDB snapshot에서 왔는가?
- [ ] Edge에 없는 셀을 삭제하지 않고 처리하는가?
- [ ] Leiden 입력에서 제외한 edge도 boundary 검증에서는 사용하는가?
- [ ] 모든 최종 cluster에 singleton 포함 크기·boundary·arc 검사를 했는가?
- [ ] Convexity와 전체 축약 그래프 legality를 구분해서 확인했는가?
- [ ] ReducedLiberty가 지원하는 arc/출력-load 조건을 만족하는가?
- [ ] Reduced Liberty와 reduced Verilog의 cell/pin mapping이 정확히 일치하는가?
- [ ] 남겨진 셀의 원본 Liberty, SDC, boundary RC가 준비되어 있는가?
- [ ] Full-design 검증과 end-to-end 시간 비교 계획이 있는가?

## 참고 자료

현재 동작의 근거는 저장소의 다음 파일이다.

- [TimingCluster.py](TimingCluster.py): 후보·domain·topological greedy·edge export.
- [TimingEdgeAggregate.py](TimingEdgeAggregate.py): pin-to-cell 합산과 ID/name 출력.
- [PlaceDB.py](PlaceDB.py), [Placer.py](Placer.py), [Timer.py](Timer.py): DB와 초기 timer 연동.
- [ops/timing/timing.py](ops/timing/timing.py): 초기 timing 입력 및 배치 중 timing 연산 인터페이스.
- [ReducedLiberty.py](ReducedLiberty.py), [ReducedVerilog.py](ReducedVerilog.py): timing abstraction 생성 및 netlist 치환.
- [TIMING_CLUSTERING.md](TIMING_CLUSTERING.md): 기존 간단 사용 안내. 구현 보장 범위는 본 문서의 주의사항과 함께 읽는다.
- [REDUCED_LIBERTY.md](REDUCED_LIBERTY.md): reduced 모델의 상세 지원 범위.

Leiden 자체의 알고리즘/API에 관한 설명은 본문의 원 논문 및 공식 문서 링크를 따른다. Domain 분리, legalization, timing weight와 검증 순서는 해당 라이브러리가 자동 제공하는 기능이 아니라 **이 회로 문제에 맞춘 설계 제안**이다.
