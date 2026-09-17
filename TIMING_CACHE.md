# PlaceDB timing DB 저장·복원

## db_option/mode로 저장·복원 결정

자동 첫 실행 캐시가 아니다. MakeDB와 같은 명시적 export/import 계약을
**timing DB**에 적용한다.

| db_option | mode | timing 동작 |
|---|---|---|
| `def` | `binary_write` | 원본을 파싱하고 `save_path/timing_cache`에 저장 |
| `def` | `read/binary_write` | 위와 동일; 복합 mode 표기도 지원 |
| `binary` | 어떤 값이든 | 저장된 timing DB만 복원 |
| `binary_wo_pos` | 어떤 값이든 | 저장된 timing DB만 복원 |
| 기타 | 기타 | 기존 텍스트 입력; timing DB 저장 없음 |

binary 모드에서는 mode에 `binary_write`가 남아 있어도 읽기만 한다.
위치 초기화는 physical DB/위치 로더의 책임이며 정적 timing 모델 복원 방법은 같다.
이전 `timing_cache_mode`와 `timing_cache_dir`는 더 이상 동작이나 경로를 제어하지 않는다.
새 설정에서는 제거한다.

## 저장 설정

```json
{
  "db_option": "def",
  "mode": "binary_write",
  "save_path": "/path/to/saved_db",
  "lib_input": ["/path/to/cells.lib", "/path/to/macros.lib"],
  "verilog_input": "/path/to/design.v",
  "sdc_input": "/path/to/design.sdc"
}
```

`PlaceDB.__call__()`의 데이터 초기화 후 저장한다. LIB/SDC 입력이 있으면
`timing_opt_flag=0`인 DB 준비 작업에서도 저장한다. timing 입력도 timing 실행
요청도 없는 순수 physical 작업은 timing DB를 만들지 않는다. timing 입력이
일부 주어졌으면 필수 LIB/Verilog 누락을 오류로 처리한다.

같은 실행에서 timing optimization도 한다면 준비한 timer를 전달하여 중복
파싱·저장하지 않는다. 별도의 `def + binary_write` 실행은 이전 DB 유무와 무관하게
원본을 다시 파싱하고 명시적으로 새 DB를 저장한다.

LIB 입력은 파일, 디렉터리, 또는 그 목록을 지원한다. 디렉터리는 하위 `.lib`를
이름순으로 탐색한다. 같은 corner에 같은 cell 모델이 중복되면 오류다.
별도 early/late 모델은 `lib_input` 대신 `early_lib_input`, `late_lib_input`을 사용한다.

## 복원 설정

```json
{
  "db_option": "binary",
  "mode": "read",
  "save_path": "/path/to/saved_db",
  "timing_opt_flag": 1
}
```

`binary_wo_pos`도 동일한 timing DB를 복원한다.
**timing 복원에는 원본 LIB·Verilog·SDC가 필요하지 않다.** 원본 탐색·원본 해시
검사를 하지 않는다. 원본이 변경되어도 저장 당시 모델을 그대로 쓰므로 변경을
반영하려면 `def + binary_write`로 다시 저장해야 한다.

DB가 없거나 손상되었거나 native backend와 호환되지 않으면 오류로 종료한다.
**원본 파싱 fallback이나 자동 재생성은 없다.** 복원은 디렉터리·잠금 파일·manifest·
모델 파일을 생성하거나 수정하지 않는다.

## 저장 내용과 유효성

`save_path/timing_cache/manifest.json`과 `model-<generation>.bin`을 저장한다.
manifest는 schema, native backend, 모델 체크섬과 저장 당시 입력의 이력을 담는다.
복원 시 원본 입력이 아닌 schema/backend/모델 자체의 체크섬을 검사한다.
이전 자동 캐시의 manifest(schema 1)는 새 계약에 포함되지 않으므로 다시 저장한다.

바이너리에는 OpenTimer가 파싱한 Liberty cell/pin 특성, timing arc와 lookup table,
단위, Verilog port/wire/instance/master/pin-net 연결, SDC command/object를 저장한다.
Liberty 모델을 instance마다 복제하지 않으며 C++ 포인터는 저장하지 않는다.
동명 LUT template은 LIB별로 분리하고 복원 시 포인터를 다시 연결한다.
OpenTimer 자체가 지원하지 않는 Liberty/SDC 기능을 추가 지원하는 것은 아니다.

현재 위치/orientation, RC tree, 계산된 delay/slew/slack, criticality 및 실행 중
net weight는 이 정적 timing DB에서 복원하지 않는다. 현재 위치로 RC/STA와 weight를
갱신한다. 기존 weight 알고리즘·주기는 유지한다. graph 재구성 및 RC/STA 시간과
메모리는 여전히 필요하다.

## 독립 실행

physical DB 없이 timing DB 부분만 저장/복원할 수 있다. 별도의 저장 강제 옵션은
없으며 JSON의 `db_option`, `mode`, `save_path`를 그대로 따른다.

```bash
cd /mnt/hdd1/XP_timing_4.1/bin
python3 dreamplace/TimingCache.py /path/to/config.json
```

명시적 저장은 `timing DB saved`, 복원은 `timing DB restored`를 출력한다.
상대 경로는 실행 디렉터리 기준이다.

## 적용 범위와 안전성

bin_sep 원본은 수정하지 않는다. physical DEF/LEF reader도 MakeDB 기반으로
연결되었으며 새 binary snapshot과 기존 bin_sep 저장물 읽기를 지원한다.
설정과 지원 범위는 [MAKEDB_INTEGRATION.md](MAKEDB_INTEGRATION.md)를 참고한다.
새 physical snapshot은 덮어쓰지 않으므로 전체 DB 재export에는 새 save_path를 사용한다.

writer lock과 원자적 manifest 교체를 사용한다. 저장 실패 시 이전 DB를 유지하고
이전 세대 모델은 자동 삭제하지 않는다. SDC 임시 JSON은 고유 경로를 사용하므로
원본 옆 사용자 JSON을 덮어쓰지 않는다. `timing_cache_dependencies`에 지정한 추가
SDC/Tcl 파일은 저장 이력에 포함되지만, 복원 시에는 그 파일들도 읽지 않는다.
Tcl 환경 변경 등도 명시적으로 다시 저장해야 반영된다.

## 검증

```bash
DREAMPLACE_INSTALL=/mnt/hdd1/XP_timing_4.1/bin \
DREAMPLACE_TIMING_CPP=/mnt/hdd1/XP_timing_4.1/bin/dreamplace/ops/timing/timing_cpp.cpython-310-x86_64-linux-gnu.so \
  python3 -m unittest test_timing_cache -v
```

명시적 저장 조건, binary 양쪽 모드의 읽기 전용 동작, 원본 없는 복원, 누락/손상 시
재생성 금지, optimization 없는 DB 저장, 중복 저장 방지, native MIN/MAX timing 및
FF/clock/RC/net-weight 비교를 검증한다.
