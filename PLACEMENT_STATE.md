# Cell-ID 기반 DREAMPlace 위치 읽기/쓰기

`Placer.py`에서 초기 좌표/orientation을 읽고, 내부 DREAMPlace 배치가 끝난 뒤
좌표/orientation을 각각의 텍스트 파일로 저장한다. CLI 옵션 또는 같은 이름의
JSON parameter를 사용한다. CLI가 JSON보다 우선한다.

```bash
cd /mnt/hdd1/XP_timing_4.1/bin
python3 dreamplace/Placer.py test/my_reduced_design.json \
  --read_posX initial_x.tsv \
  --read_posY initial_y.tsv \
  --read_orient initial_orient.tsv \
  --write_posX results/reduced/final_x.tsv \
  --write_posY results/reduced/final_y.tsv \
  --write_orient results/reduced/final_orient.tsv
```

`--read_psoX`도 `--read_posX`의 오타 호환 별칭이다.
`--wrtie_orient`는 `--write_orient`의 오타 호환 별칭이다. 각 옵션은 독립적이며,
지정하지 않은 입력 항목은 설계 파일 값을 사용한다. 출력 옵션 없이도 최종 지표를
계산하여 로그에 표시하며, orientation 파일 기록만 `write_orient`로 제어한다.

## 파일 형식과 ID

사용자가 요청한 형식인 `cell_id 값` 두 열이다. 입력 구분자는 공백 또는 탭,
출력은 탭이다. 입력 행 순서는 자유이며 출력은 ID 오름차순이다.

X 파일:

```text
cell_id posX
0 1200.5
1 1480
2 2010
```

Y 파일은 `cell_id posY`, orientation 파일은 다음과 같다.

```text
cell_id orient
0 N
1 FN
2 FS
```

헤더는 생략할 수 있다. `#` 주석과 빈 줄을 허용한다. 제공하는 **각 입력 파일은
모든 movable cell ID를 포함해야 한다**. Fixed macro/shape/IO 행은 선택 사항이며
현재 값과 같은 경우만 허용한다. 이 인터페이스는 fixed 객체를 이동하거나
회전시키지 않는다. Unknown/중복 ID, 누락 movable ID, NaN/Inf, 잘못된 orientation은
오류로 처리한다. 입력 검증이 끝나기 전에 배치 배열을 변경하지 않는다.

ID는 `PlaceDB.node_names[cell_id]`의 **physical node ID**이다. 동일한 physical DB에서
`timing_edges.csv`에 사용하는 ID와 같다. 다음 ID와 혼동하면 안 된다.

- clustering 알고리즘의 `cluster_id`
- 원본 Verilog 순서나 raw C++ node ID
- filler ID (지원하지 않음)
- 축약 전 설계의 cell ID (reduced 설계에 그대로 적용하지 않음)

출력은 movable, fixed shape, IO를 포함한 모든 physical node를 담고 filler는 제외한다.
첫 출력 파일 이름에 `.cells.tsv`를 붙인 ID–이름 대응표도 자동 생성한다.
위 명령에서는 `final_x.tsv.cells.tsv`가 생성된다. 매핑에는 원본 macro 대신
`.DREAMPlace.Shape<N>`이 나올 수 있다.

자동 생성 파일에는 ID 순서별 셀 이름의 SHA-256 checksum 주석이 들어간다.
재입력 시 이름/ID 순서가 다르면 중단한다. 수동 파일에서 checksum을 생략하면
경고를 출력하며, ID가 올바른지는 사용자가 확인해야 한다. Checksum은 이름/순서만
검사하며 Liberty corner나 전체 connectivity/geometry 동일성까지 보장하지 않는다.

## 좌표 단위와 초기화

X/Y는 **cell bounding box의 좌하단 좌표**이며 DREAMPlace의 shift/scale을 적용하기
전 원래 PlaceDB 단위이다. LEF/DEF 설계에서는 LEF database unit 기반이고, Bookshelf는
원래 Bookshelf 좌표 단위이다. Micron이나 DREAMPlace 내부 정규화 좌표를 직접 넣지 않는다.
LEF와 DEF의 DBU가 다를 수 있으므로 단순히 DEF 숫자와 같다고 가정하지 않는다.
출력은 `unscale_pl()`로 내부 shift/scale을 역변환하므로 그대로 재입력할 수 있다.

입력은 `PlaceDB.read()` 직후, density/filler/scale 초기화 전에 적용한다. X 또는 Y를
읽으면 `random_center_init_flag=0`, `gp_noise_ratio=0`으로 설정해 초기 좌표가
랜덤 중앙 초기화나 시작 noise로 덮어써지지 않도록 한다. 이후의 최적화 이동,
die 경계 보정, legalization 등은 정상적으로 진행한다. Filler의 초기화는 별개다.

Orientation은 라벨만 바꾸지 않는다. Canonical N으로 읽힌 movable pin offset에
회전/반사를 적용하고 width/height 및 raw DB orientation도 갱신한다. 지원 표기는
`N S FN FS E W FE FW`이다. 90도 orientation `E/W/FE/FW`는 현재 row 기반 legalizer의
제약 때문에 `legalize_flag=0`, `detailed_place_flag=0`인 경우에만 입력을 허용한다.
Fixed/IO의 `UNKNOWN`은 기존 값 확인용으로 허용하지만 movable 입력에는 허용하지 않는다.
배치 종료 시 row 방향에 맞춘 flip이 생길 수 있으므로 출력 orientation은 최종
raw DB에서 읽으며, 입력 orientation이 반드시 그대로 유지되는 것은 아니다.

## 출력 범위와 파일 보호

출력은 내부 global placement 및 선택적으로 실행한 내부 legalization/detail placement가
끝난 시점이다. 별도 `detailed_place_engine`으로 실행하는 외부 placer의 결과는 포함하지
않는다. 이 옵션으로 최종 결과를 관리할 때는 `detailed_place_engine`을 빈 문자열로
설정하는 것을 권장한다.

출력 디렉터리는 자동 생성한다. 기존 파일을 덮어쓰지 않으며 입력과 출력, 각 출력
파일 경로는 서로 달라야 한다. 새 실행에는 새 출력 파일명을 사용한다. `.cells.tsv`
대응표도 같은 덮어쓰기 방지 규칙을 따른다.

## Reduced 설계에 적용할 때

Reduced Liberty와 reduced Verilog는 timing 모델/논리 연결을 정의한다. DREAMPlace에는
추가로 cluster 크기·pin geometry가 있는 **대응 LEF와 DEF**, 또는 이에 준하는
Bookshelf physical DB가 필요하다. 이 옵션은 reduced physical DB를 자동 생성하거나
원본 cell 위치를 cluster 위치로 변환하지 않는다. 입력 좌표 ID도 반드시 그 reduced
physical DB에서 얻어야 한다.

## Orientation 출력에 포함되는 최종 평가

`write_orient` 유무와 관계없이 `NonLinearPlace`의 내부 legalization/detail
placement와 `placedb.apply()` 이후 **최종 위치**에서 항상 평가하고 로그에 표시한다.
GP 중간/best metric이나 이전 STA 값을 재사용하지 않는다. Global placement가
꺼져 있어도 평가한다. 이때 timing net weight는 갱신하지 않는다.

`write_orient`를 지정한 경우에만 orientation 파일을 쓰고, 셀 행 다음에 아래처럼
지표 주석 행을 추가한다 (숫자는 예시). 미지정 시 별도 지표 파일은 만들지 않는다.

```text
# metric	wns	-12.5	ps_late
# metric	tns	-80	ps_late
# metric	hpwl	123456	weighted_original_db_length
# metric	overflow	0.012	ratio
# metric	max_density	1.1	ratio
# metric	congestion_max	2.3	reference_congestion
# metric	congestion_total	123.4	reference_congestion
# metric	macro_congestion_max	2.1	reference_congestion
# metric	macro_congestion_total	20.3	reference_congestion
# metric	iteration	100	count
```

- WNS/TNS: 최종 위치에서 RC 재구성 후 STA 갱신. Late/setup 기준, 단위는 ps.
  OpenTimer의 library time unit을 이용해 ps로 변환한다. TNS는 endpoint마다
  rise/fall 중 worst를 사용한다. 기존 iteration 로그의 `1e3 ps`/`1e5 ps`
  표기와 단위가 다르므로 숫자를 그대로 비교하지 않는다.
- STA timer가 없으면 WNS/TNS는 `NA`. 수치를 얻으려면 `timing_opt_flag=1`과
  유효한 timing 입력 또는 저장된 timing DB가 필요하다.
- HPWL: 기존 DREAMPlace HPWL operator와 최종 net weight를 사용하고,
  `scale_factor`를 역적용한 원래 DB 길이 단위로 기록한다. Unweighted HPWL이 아니다.
- Overflow: 전체 die의 density overflow / movable cell area. Region별 vector가
  아니며 filler는 제외한다. 최종 geometry로 새 overflow operator를 생성한다.
- Congestion: 아래 참고 코드의 추정치이며 실제 detailed routing overflow가 아니다.

`#` 주석이므로 이 파일을 다시 `--read_orient`로 입력해도 된다. 최종 평가가
실패하면 성공한 것처럼 가짜 0 값을 저장하지 않고 오류로 중단한다.

### 원본 congestion 코드

사용자가 지정한 경로의 실제 철자는 `/mnt/hdd1/XP_shared_memory/bin/dreamplace`다.
2026-09-17 기준 그곳의 `NonLinearPlace.py`는 `PlaceDB.calc_congestion_numba` 또는
`calc_congestion_map_cuda`를 호출한다. 계산 본체와 전이 의존 함수 28개를
`SharedCongestion.py`에 **함수 텍스트 그대로** 복사했다. 원본 파일은 수정하지 않았고,
실행 시 원본 프로젝트를 import하지도 않는다. `test_final_placement_metrics.py`에서
복사된 모든 함수와 원본 텍스트의 동일성을 검사한다.

`FinalPlacementMetrics.py`는 데이터 연결만 담당한다: pin CSR, 현재 net weight,
macro 종류/범위, blockage, 좌표 단위를 원본 인터페이스에 맞춘다. Native Bookshelf는
평가용 뷰만 원본의 타입 순서로 배열하며 실제 DB의 ID/배치를 바꾸지 않는다.
Native movable macro 판별은 기존 `movable_macro_mask`를 사용하고 fixed terminal은
macro로 취급한다. MakeDB는 명시적인 std/macro/blockage count를 그대로 사용한다.
최종 row flip이 있으면 pin offset을 갱신해 HPWL·RC·congestion에도 반영한다.

원본과 같은 JSON 설정을 사용할 수 있다 (미지정 시 원본 함수의 기본값 사용).

```json
{
  "congestion_backend": "auto",
  "congestion_calculation_method": "rudy_pin_bbox",
  "congestion_rudy_exact_max_bbox_bins": 4096,
  "congestion_rudy_max_source": "hv_max",
  "congestion_rudy_sum_source": "hv_max",
  "congestion_macro_weight": 2.0,
  "congestion_placement_blockage_weight": 2.0,
  "congestion_routing_blockage_weight": 2.0
}
```

원본의 `legacy` cell-bbox 방식도 유지한다. 기본 RUDY는 pin bbox, net weight,
capacity, macro/blockage 가중치를 사용한다. `MODCSA` 접두사 net 제외 및 큰 bbox
근사 처리도 원본 그대로다. 격자는 원본과 같이 site width × row height 단위이므로
큰 설계에서는 최종 평가에 상당한 메모리가 필요할 수 있다. Numba 최초 실행에는
JIT 시간이 추가된다. **원본의 CUDA entrypoint 자체가 Numba 경로를 호출**하므로
`cuda` 선택이 새로운 GPU 가속을 의미하지는 않는다.

새 `def + binary_write` 저장본은 blockage/변환된 fixed macro 메타데이터도 보존한다.
기존 저장본에 이 메타데이터가 없으면 경고한다. 그 경우 node로 남은 blockage는
반영하지만 routing-only blockage 등은 빠질 수 있으므로 완전한 비교에는 새 저장본이
필요하다. 기존 저장본을 자동으로 수정하거나 덮어쓰지는 않는다.

## 테스트

```bash
DREAMPLACE_INSTALL=/mnt/hdd1/XP_timing_4.1/bin \
  python3 -m unittest test_placement_state -v
```

순서 독립 입력, 오류 검증, pin orientation 변환, ID checksum, native-unit 출력,
덮어쓰기 방지와 작은 실제 DREAMPlace 설계의 초기 위치 유지 및 재시작을 검증한다.
