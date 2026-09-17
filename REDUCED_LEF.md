# 중앙 boundary pin을 갖는 클러스터 LEF

`ReducedLEF.py`는 **배치용 첫 버전**의 LEF를 생성한다. 모든 input/output pin을
클러스터 중앙에 놓으며, 실제 라우팅/DRC용 hard macro abstract가 아니다.
원본 프로젝트와 LEF/Liberty/Verilog 입력 파일은 수정하지 않는다.

## 사용법

```bash
cd /mnt/hdd1/XP_timing_4.1/bin
python3 dreamplace/ReducedLEF.py \
  --input results/reduced/cluster_mapping.jsonl \
  --sizes cluster_sizes.tsv \
  --tech-lef benchmarks/iccad2015.ot/superblue1/superblue1.lef \
  --pin-layer metal2 \
  --output results/reduced/cluster_cells.lef
```

위의 mapping/크기 파일은 사용자의 실제 데이터로 준비해야 한다. `--tech-lef`는
선택 옵션이다. 지정할 경우에는
해당 설계의 routing layer 정의를 가진 LEF를 지정한다. 예제의 superblue LEF를
다른 공정 설계에 그대로 사용하지 않는다. `--tech-lef`는 여러 번 지정할 수 있다.
`--input`의 별칭은 `--mapping`이다. OpenTimer 실행이나 GPU는 필요하지 않다.

### tech.lef 없이 생성

```bash
python3 dreamplace/ReducedLEF.py \
  --input /실제경로/cluster_mapping.jsonl \
  --sizes /실제경로/cluster_sizes.tsv \
  --pin-layer metal2 \
  --output /실제경로/cluster_cells.lef
```

`--tech-lef`를 생략하면 기본 pin 크기는 **0.1 × 0.1 µm**이다. 공정 규칙에서
얻은 값이 아닌 배치용 임시 형상이며 `--pin-width 0.2 --pin-height 0.2`처럼
변경할 수 있다. Pin은 여전히 클러스터 중앙에 놓인다. 클러스터보다 큰 pin은
거부되므로 아주 작은 클러스터에는 더 작은 pin 크기를 명시한다.

이 모드에서는 layer 존재 여부/TYPE ROUTING/MINWIDTH 및 기존 LEF의 master 이름
충돌을 검증하지 않는다. 경고를 CLI, LEF 주석, summary JSON에 기록하며
`technology_validated=false`로 표시한다. Layer 정의나 RC 공정 수치는 만들지 않는다.
DATABASE MICRONS도 임의로 지정하지 않고 UNITS 블록을 생략한다.
**SIZE와 RECT는 계속 µm**이며, `--size-unit dbu --dbu-per-micron ...` 입력 환산은
기술 LEF 없이도 동작한다.

파일 생성에 tech LEF가 불필요하다는 의미이지, 모든 downstream reader나 routing
도구에서도 기술 정보가 불필요하다는 의미는 아니다. 특히 native reader는 layer
정의를 다른 입력 LEF에서 요구할 수 있다. `--pin-layer`에는 사용 중인 원본 cell
LEF의 실제 pin layer 이름을 지정한다. 파일 이름이 tech.lef일 필요는 없으며,
기술 정의가 통합된 일반 LEF도 `--tech-lef`로 지정할 수 있다.

## 입력 1A: cell_id / cluster_id 텍스트 (MakeDB 저장 결과 사용)

다음 membership 파일을 `--input`에 직접 지정할 수 있다. 헤더가 없어도 된다.
탭/쉼표/공백 형식을 읽으며 헤더가 있다면 `cell_id`, `cluster_id` 열을 사용한다.

```text
25793	1
47529	1
153939	1
```

실제 superblue1 경로에서 다음처럼 실행한다. 폴더 이름은 `sace`가 아닌 `save`다.

```bash
cd /mnt/hdd1/XP_timing_4.1/bin/results/superblue1/timing_edges_protected
python3 /mnt/hdd1/XP_timing_4.1/bin/dreamplace/ReducedLEF.py \
  --input superblue1_supercell_clustered.txt \
  --cell-names cell_names.tsv \
  --saved-db save \
  --sizes superblue1_supercell_size.txt \
  --pin-layer metal2 \
  --output supercell/LEF/cluster_cells.lef
```

이 명령도 기존 출력이 있으면 덮어쓰지 않으므로 새 경로를 지정한다.
`--input-format auto`가 기본값이다. 필요하면 `--input-format membership` 또는
`--input-format mapping`으로 명시한다. `--sizes`는 기존과 동일하게 기본 µm, 탭 구분이다.

참조 관계:

| 입력 | 용도 |
| --- | --- |
| membership | cell_id → cluster_id |
| `--cell-names` | cell_id → 원본 cell_name |
| `save/cells_info.json` | cell_name → macro_id (LEF master/type) |
| `save/lef_info.json` | macro_id → 각 pin의 INPUT/OUTPUT 방향 |
| `save/netlist_info.json` | net → cell/pin 연결 전체 |
| `save/ext_pin_info.json` | top-level IO 방향 |

**membership의 cell_id는 반드시 그 ID를 만들 때의 cell_names 표로 해석한다.**
`save/node_names.txt`의 행 번호나 `cells_info.json`의 순서로 대체하지 않는다.
현재 superblue1의 cell_names.tsv와 save/node_names.txt는 실제 순서가 다르다.
이후 연결은 ID가 아닌 cell_name으로 MakeDB 정보와 결합한다.

`--saved-db` 대신 `--cells-info`, `--lef-info`, `--netlist-info`, `--ext-pin-info`로
파일들을 개별 지정하거나 특정 파일만 덮어 지정할 수 있다. 외부 port를 만나는
net에는 ext_pin_info가 필요하다. tech.lef는 여전히 선택 옵션이다.

### 경계 pin 판별

각 net에서 LEF 방향을 이용해 driver 하나와 sink들을 찾는다. Top-level INPUT은
driver, OUTPUT은 sink로 해석한다. POWER/GROUND net은 논리 pin에서 제외한다.

- driver가 클러스터 밖이고 내부 sink가 있으면 클러스터 INPUT.
- driver가 클러스터 안이고 외부 sink가 있으면 클러스터 OUTPUT.
- 모든 연결이 클러스터 내부이면 외부 LEF pin을 만들지 않는다.
- 외부 sink와 내부 sink가 섞인 fanout도 하나의 OUTPUT pin으로 표현한다.

INPUT/OUTPUT 각각 net 이름을 정렬하여 `I0,I1,...`, `O0,O1,...`을 매긴다.
master 이름은 `TC_<cluster_id>`이다. 이는 ReducedLiberty의 번호 규칙과 같지만,
DEF 저장 연결/LEF 방향과 Verilog/Liberty가 다르면 같은 결과를 보장할 수 없다.
STA에 사용하기 전에 실제 characterized mapping과 pin/net 일치를 확인해야 한다.

ID/name 누락, master/pin 누락, 중복 연결, driver가 0개/여러 개인 net,
INOUT/방향 불명, input/output 경계가 없는 cluster는 오류로 중단한다.
FF/clock domain/convexity를 LEF 정보만으로 인증하지 않으며, timing delay도 계산하지 않는다.
생성 mapping에는 `timing_characterized=false`를 명시한다.
`timing_edges.csv`는 필터링된 연결만 포함할 수 있으므로 전체 경계 복원에 사용하지 않는다.

### 추가 출력 및 원본 LEF 파일 추적

membership 모드에서는 기본 LEF와 summary 외에 다음을 만든다.

- `cluster_cells.lef.mapping.jsonl`: 클러스터 멤버·master·경계 pin/net 정보.
  동일 연결을 검증한 ReducedVerilog/ReducedDEF의 mapping 입력으로 사용할 수 있다.
  characterized Liberty 자체가 생성되는 것은 아니다.
- `cluster_cells.lef.cells.tsv`: 선택된 멤버를 **cell_id 숫자 오름차순**으로 정렬한 표.
  열은 `cell_id`, `cell_name`, `cluster_id`, `macro_id`, `source_lef`이다.

`macro_id=INV_Z10`은 **LEF 셀 타입 이름이지 LEF 파일명이 아니다.** 기존
cells_info.json/lef_info.json에는 원본 파일 출처가 없으므로 파일명을 추측하지 않는다.
파일 경로도 기록하려면 원본 셀 LEF를 반복 지정한다.

```bash
--source-lef /path/cells_a.lef --source-lef /path/cells_b.lef
```

이 옵션은 일반적인 독립 줄의 `MACRO <name>` 선언을 확인해 파일 출처를 기록한다.
여러 파일에 같은 master가 있거나 선택된 master가 없으면 중단한다. 기술 규칙을
검증하는 `--tech-lef`와는 별개이며, 생략하면 source_lef 열은 빈칸(JSON에서는 null)이다.

셀/net JSON은 항목별로 순차 디코딩해 불필요한 전체 nested JSON 객체를 유지하지 않는다.
다만 클러스터 mapping과 LEF 출력 모델은 메모리에 유지하므로 대규모 변환에는
충분한 메모리가 필요하다. 원본 save 파일은 변경하지 않는다.

## 입력 1B: 기존 boundary pin mapping JSONL

`--input`은 기존 `ReducedLiberty.py`가 생성한 **`cluster_mapping.jsonl`**이다.
한 줄에 클러스터 하나의 JSON 객체가 있으며 구성 셀, Liberty master 이름,
input/output pin 이름 및 연결 net을 포함한다.

```json
{"cluster_id":7,"liberty_cell":"TC_7","members":[{"cell_id":0,"cell_name":"u0","original_master":"INV"}],"inputs":[{"pin":"I0","net":"a","original_pins":["u0:A"]}],"outputs":[{"pin":"O0","net":"y","original_pin":"u0:Y"}]}
```

`.lib` 파일을 이 옵션에 직접 넣지는 않는다. 단순한 membership 파일은 위의
MakeDB 메타데이터 옵션과 함께 사용한다. 연결 정보 없는 membership만으로
input/output pin을 결정할 수 없기 때문이다. 기존 mapping을 사용하면 LEF/Liberty/reduced Verilog의
`TC_7`, `I0`, `O0` 이름이 일치한다. Pin 번호를 새로 매기지 않는다.
같은 디렉터리에 Liberty 생성 `manifest.json`이 있으면 `status=complete`인지 검사한다.

## 입력 2: 클러스터 전체 폭·높이

`--sizes`의 기본 구분자는 **탭**, 기본 단위는 **µm**이다.

```text
cluster_id	width	height
7	40	20
8	30	30
```

폭·높이는 구성 원본 셀 하나하나의 크기가 아니라 **합쳐진 클러스터 전체 크기**다.
코드는 크기를 그대로 사용하며, 면적 합산·이용률 보정·aspect ratio 선택을 하지 않는다.
숫자는 유한한 양수여야 하며, mapping과 크기표의 cluster ID 집합이 정확히 같아야 한다.
헤더가 있으면 열 이름으로 찾고, 헤더가 없으면 앞의 세 열을 위 순서로 읽는다.
빈 줄과 `#` 주석을 허용하며, 중복 ID와 누락 크기는 오류다.

다른 구분자:

```bash
--delimiter comma   # CSV
--delimiter space   # 공백으로 구분된 파일
```

크기가 원래 DEF DBU 단위라면 단위 환산을 명시한다. 이 숫자는 입력 크기표의
단위이며 LEF 파일의 DATABASE MICRONS와 별개다.

```bash
--size-unit dbu --dbu-per-micron 1000
```

이 경우 `width=40000 height=20000`은 LEF에서 `SIZE 40 BY 20`이 된다.

## Pin 형상과 layer

- `--pin-layer`: 사용할 layer 이름. 생략할 수 없다. 기술 LEF 제공 시 `TYPE ROUTING`을 검사한다.
- `--pin-width`: rectangle 폭(µm). 생략하면 layer의 `WIDTH`, 없으면 `MINWIDTH` 사용. 기술 LEF 미제공 시 0.1 µm.
- `--pin-height`: rectangle 높이(µm). 생략하면 pin width와 같다.
- 제공한 기술 LEF에 폭 정보가 없으면 `--pin-width`를 직접 지정해야 한다.
- 명시된 `MINWIDTH`보다 작은 pin 및 클러스터 밖으로 나가는 pin은 거부한다.

폭 W, 높이 H, pin 폭 pw, 높이 ph에 대해 모든 pin에 다음 rectangle을 사용한다.

```text
((W-pw)/2, (H-ph)/2) ~ ((W+pw)/2, (H+ph)/2)
```

생성 master는 `CLASS BLOCK`, `ORIGIN 0 0`, `SYMMETRY X Y`이며, pin은 mapping의
방향에 따라 `DIRECTION INPUT/OUTPUT`, `USE SIGNAL`로 기록한다. `SITE`, 전원 pin,
OBS는 임의로 만들지 않는다. Routing layer 정의 자체는 복사하지 않으므로 실행할 때
기술 정의가 필요한 reader에서는 해당 정의를 가진 LEF도 함께 읽어야 한다.
`useless_layer_list`로 선택 layer를 제외하면
현재 MakeDB에서 pin이 필터링될 수 있으므로 배치 설정도 확인한다.

기술 메타데이터 reader는 일반적인 명시적 `LAYER`/`UNITS` 블록을 읽고 macro와
quoted property를 건너뛴다. LEF58 property 안의 규칙을 해석하는 완전한 기술 LEF
검증기는 아니다. Manufacturing grid snap이나 spacing/minarea 검증도 하지 않는다.

## 출력과 적용

`--output cluster_cells.lef`이면 두 파일을 생성한다.

- `cluster_cells.lef`: cluster ID 숫자 오름차순으로 정렬한 LEF macro들
- `cluster_cells.lef.summary.json`: 크기·중앙 좌표·pin rectangle·pin 이름·구성 셀 수·입력 경로·모델 한계

어느 출력이든 이미 존재하면 덮어쓰지 않고 중단한다. 입력과 출력 경로가 겹쳐도 중단한다.
배치에서는 남아 있는 원본 셀의 LEF 및 reader가 요구하는 기술 정의와 함께 새 LEF를 읽는다.
새 LEF만으로 원본 standard-cell library 전체를 대체하지 않는다.

**Reduced DEF는 별도로 필요하다.** 클러스터 인스턴스 master와 pin 연결이
reduced Verilog 및 이 LEF와 같아야 한다. 이 도구는 DEF를 생성하지 않는다.

모든 pin이 겹치므로 실제 라우팅에는 사용할 수 없다. 중앙 좌표로 외부 RC를
근사하고, 내부 delay는 reduced Liberty에 의존한다. 현재 reduced Liberty의 내부
배선 RC 생략 문제를 이 LEF가 보완하지는 않는다. 초기 후보 탐색 후 원본 셀로
복원하여 timing을 재검증해야 한다.

## 바로 실행할 수 있는 작은 예제

소스 디렉터리의 `examples/reduced_lef/`에 테스트용 mapping·크기표·기술 LEF가 있다.
데모 기술 LEF는 실제 공정 파일이 아니다.

```bash
cd /mnt/hdd1/XP_timing_4.1/dreamplace
python3 ReducedLEF.py \
  --input examples/reduced_lef/cluster_mapping.jsonl \
  --sizes examples/reduced_lef/cluster_sizes.tsv \
  --tech-lef examples/reduced_lef/demo_tech.lef \
  --pin-layer metal2 \
  --output /tmp/demo_cluster_cells.lef
```

테스트: `python3 -m unittest test_reduced_lef -v`.
