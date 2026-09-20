# 원본 timing / 축약 placement DB의 ID mapping

`PlacementTimingMapping.py`는 두 DB의 **확정된 배열 ID**와 ClusterPlacement의
이름 기반 대응표를 연결한다. 원본 DB/축약 DB를 변경하거나 새로 생성하지 않는다.
GPU, OpenTimer, 원본 LEF/DEF 재파싱 없이 동작한다.

## 전제와 ID 의미

- 원본 timing geometry DB와 축약 placement DB를 먼저 저장한다.
- 각 `save_path/physical_db`의 MakeDB schema 1 NPY 배열을 읽는다.
- `--timing-db`는 원본 물리 연결/이름 배열을 가진 saved DB이다.
  **`timing_cache/model.bin`만 지정해서는 안 된다.** 원본 OpenTimer 파싱 캐시와
  원본 node/pin/net의 물리 ID 배열은 별도 정보다.
- 입력 ID는 `node_names`, `pin_names`, `net_names` 배열의 0-based index이다.
  OpenTimer 내부 ID나 Leiden membership의 cell_id를 이 숫자와 혼동하지 않는다.
- 원본 PlaceDB는 이미 CLOCK/degree-one net 등 일부 입력을 제외했을 수 있다.
  이 도구는 누락된 원본 timing topology를 복원하지 않는다. 원본 TimingDB에
  모든 필요한 신호가 들어 있는지 확인하는 일은 별도 단계다.
- 기존 legacy saved DB의 `node_names.txt`, `pin_names.txt`, `net_names.txt`,
  `pin2node_map.txt`, `pin2net_map.txt`도 지원한다. pickle은 읽지 않는다.
  legacy 배열은 MakeDB가 그대로 복원하는 ID 순서여야 한다.

## 실행

`XP_timing_4.1/bin`에서, 예를 들어 축약 DB를 `PLACEMENT_U0.7/save`에 이미
저장했다면 다음과 같이 실행한다. 이 `save` 경로는 **사용자가 생성할 경로의 예시**다.
LEF/DEF만 생성한 상태에서는 아직 숫자 ID mapping을 만들 수 없다.

기본 superblue1 경로에서 최초 실행은 다음 두 단계이다:

```sh
# 명시적인 DEF + binary_write: CPU에서 물리 DB만 저장
sh ./run_prepare_placement_db.sh

# 두 저장 DB를 읽어 mapping만 생성
sh ./run_placement_timing_mapping.sh
```

첫 스크립트는 `PLACEMENT_U0.7/reduced.def`와 complete replacement
`PLACEMENT_U0.7/placement.lef`를 읽어 `PLACEMENT_U0.7/save`에 저장한다.
GPU/배치/STA는 실행하지 않는다. 기존 save 경로는 비어 있어도 덮어쓰지 않는다.
`TC_PHYSICAL`, `TC_PLACEMENT_DB`, `TC_THREADS`로 지정 가능하다.
custom parser 설정이 필요하면 `sh run_prepare_placement_db.sh --config config.json`
처럼 추가한다. GPU/timing/source 경로와 read/write 위치 override는 무시한다.
일반 데이터는 `PreparePlacementDB.py --def-input ... --lef-input placement.lef
--output new_save_path`로 준비할 수 있다. LEF 디렉토리는 하위 폴더도 탐색한다.

mapping 스크립트는 DB가 없다고 자동 재생성하지 않고 준비 명령을 안내한다.

```sh
TC_PLACEMENT_DB=results/superblue1/timing_flow_v2/PLACEMENT_U0.7/save \
  sh run_placement_timing_mapping.sh
```

직접 실행:

```sh
python3 dreamplace/PlacementTimingMapping.py \
  --timing-db results/superblue1/timing_edges_protected/save \
  --placement-db results/superblue1/timing_flow_v2/PLACEMENT_U0.7/save \
  --cluster-data results/superblue1/timing_flow_v2/PLACEMENT_U0.7 \
  --output results/superblue1/timing_flow_v2/PLACEMENT_U0.7/ID_MAPPING
```

`--cluster-data`에는 완료된 `ClusterPlacement.py`의 `manifest.json`,
`cell_mapping.tsv`, `pin_mapping.tsv`, `net_mapping.tsv`가 필요하다.
새 출력 디렉토리만 허용한다. 반복 실행 시 출력 경로를 바꿔야 한다.
`--no-tsv`는 사람이 보는 큰 TSV를 생략한다. binary 배열은 항상 생성한다.
`--temp-dir`로 SQLite index의 임시 디스크를 지정할 수 있다.
대규모 입력은 임시 디스크 수 GB가 필요할 수 있으며 작업 후 자동 정리한다.

## 축약 DB를 아직 저장하지 않았다면

기존 DB 준비 절차대로 축약 DEF + complete replacement placement LEF를 읽어서
`db_option=def`, `mode=binary_write`, 새 `save_path`로 내보낸다.
축약 물리 DB 준비에 원본 Verilog/Liberty/SDC를 붙여 원본 timing cache를
동일 DB인 것처럼 생성하지 않는다. 두 DB는 서로 다른 저장 경로를 사용한다.

CPU에서 물리 DB만 명시적으로 내보내는 기존 adapter API 예시:

```python
from Params import Params
import MakeDBAdapter

p = Params()
p.db_option = "def"
p.mode = "binary_write"
p.def_path = ".../PLACEMENT_U0.7/reduced.def"
p.lef_dir_path = [".../PLACEMENT_U0.7/placement.lef"]
p.save_path = ".../PLACEMENT_U0.7/save"  # 새 경로
_, db = MakeDBAdapter.read(p)
MakeDBAdapter.save(db, p)
```

이는 본 mapping 생성기가 자동으로 수행하는 작업이 아니다. 기존 DB가 있으면
`binary`/`binary_wo_pos`로 복원하며 기존 파일을 덮어쓰지 않는다.

## 출력 배열

모든 배열은 pickle 없는 `int64` NPY이며 이름은 다음과 같다.

| 파일 (`.npy`) | index → value |
|---|---|
| `timing_node_to_placement_node` | 원본 node → 축약 node; 다대일 가능 |
| `timing_node_cluster_id` | 원본 node → cluster_id; 미병합/IO는 -1 |
| `timing_pin_to_placement_pin` | 원본 pin → 축약 pin; 다대일 또는 -1 |
| `timing_pin_to_placement_node` | 원본 pin → 위치를 따라갈 축약 node; 누락 pin에도 존재 |
| `timing_net_to_placement_net` | 원본 net → 축약 net 또는 -1 |
| `placement_net_to_timing_net` | 축약 net → 원본 net; 일대일 |
| `placement_node_to_timing_nodes` / `_start` | 축약 node별 원본 node 목록(CSR) |
| `placement_pin_to_timing_pins` / `_start` | 축약 pin별 원본 pin 목록(CSR) |

TSV는 `node_mapping.tsv`, `pin_mapping.tsv`, `net_mapping.tsv`다.
각 원본 ID, 이름, 축약 ID, 이름, 상태를 기록한다. node에는 cluster ID,
pin에는 좌표를 따라갈 placement node ID도 기록한다.

**`-1`은 마지막 원소가 아니라 명시적인 대응 없음이다.** NumPy에서
`reduced_values[mapping]`을 무조건 수행하면 -1이 마지막 원소를 가리키므로
반드시 `mapping >= 0` mask를 적용해야 한다.

내부 net이 cluster의 VP 하나로 축약되어 MakeDB에서 빠지면:

```text
timing_net_to_placement_net[internal_net] = -1
timing_pin_to_placement_pin[internal_pin] = -1
timing_pin_to_placement_node[internal_pin] = cluster의 node ID
```

원본 pin의 RC 좌표를 만드는 데 placement pin ID가 반드시 필요한 것은 아니다.
cluster node와 중심 좌표를 사용할 수 있다. 다만 미병합 pin에는 원본 offset을
적용하는 별도 좌표 변환이 필요하다. 이 도구는 실제 좌표 변환을 구현하지 않는다.

역방향 node 목록 예:

```python
s = maps["placement_node_to_timing_nodes_start"]
members = maps["placement_node_to_timing_nodes"][s[node_id]:s[node_id + 1]]
```

## 복원과 검증

```python
from PlacementTimingMapping import load_mapping

maps = load_mapping(mapping_dir, original_db, reduced_db)
# 위 함수가 fingerprint, NPY checksum, range, 역방향 CSR, pin 소속을 검증한다.
# 두 인자는 저장 디렉토리 또는 현재 로딩된 DB 객체 모두 가능하다.

# 차후 timing 연동에서의 weight 전달 예시 (현재 배치 loop에 연결되지는 않음):
index = maps["placement_net_to_timing_net"]
reduced_db.net_weights[:] = original_db.net_weights[index]
```

`manifest.json`은 두 DB의 **순서가 포함된 이름 배열 + pin2node/pin2net**에
대한 SHA-256, mapping 입력 파일 SHA-256, 각 NPY SHA-256을 저장한다.
문자열 배열이 bytes/Unicode 또는 integer dtype이 int32/int64로 바뀌어도
같은 ID/연결이면 fingerprint가 같다. 위치, 방향, weight, 크기는 ID 식별용
fingerprint에서 제외한다. 이것은 Liberty/SDC나 geometry의 유효성 검증이 아니다.

DB ID 순서/연결이 달라지면 복원을 거절한다. 새 mapping을 생성해야 하며,
원래 저장 배열과 다른 순서를 사용하는 runtime에서는 로딩된 실제 DB 객체로
`build(original_db, reduced_db, cluster_data_dir, output_dir)`를 호출한다.
pin 이름과 pin 소속 배열이 서로 일치하도록 정렬된 DB만 허용한다.

오류로 처리하는 경우:

- 중복 이름/대응표, net 병합/이름 변경.
- 원본 node의 대응 위치 없음, 서로 다른 cluster가 같은 placement node로 연결됨.
- 원본 pin의 net/node와 대응표 불일치, 축약 pin의 소속 net/node 불일치.
- nontrivial net 또는 그 pin이 축약 DB에서 사라짐.
- 원본 source가 없는 축약 node/pin/net.

원본 DB에 없는 sidecar 행은 reader에서 필터된 입력일 수 있으므로 개수를
manifest에 기록한다. 원본 DB에 **존재하는** 항목의 누락은 묵인하지 않는다.
유일한 net/pin 누락 허용은 sidecar의 reduced_degree=1이고 원본 pin들의
node mapping도 실제 한 node로 수렴하는 경우다.
IO와 변하지 않은 물리 node는 이름으로 identity 대응한다. sidecar에 없는
synthetic net/pin은 양쪽 MakeDB metadata에서 명시적으로 placement-only로
표시된 동일 net에 한해 검증 후 identity 대응한다.

## 테스트

```sh
python3 -B -m unittest test_placement_timing_mapping -v
```

순서가 서로 다른 DB, 다대일 pin/node, 내부 net 제외, checksum/순서 변경,
잘못된 owner/누락/중복, legacy TXT, CLI, 실제 MakeDB binary round-trip을 검증한다.
이 단계는 mapping 생성/검증만 구현하며 두 DB의 RC/STA feedback loop는 아직 연결하지 않는다.
