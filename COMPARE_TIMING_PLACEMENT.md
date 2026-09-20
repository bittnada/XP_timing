# 원본 / two-DB 배치 비교

`CompareTimingPlacement.py`는 배치를 다시 실행하거나 net weight를 변경하지 않고,
저장된 두 최종 DEF를 같은 **원본 pin/net ID** 기준으로 비교합니다.
입력 DB, mapping, DEF는 변경하지 않습니다. 기존 출력 디렉터리도 덮어쓰지 않습니다.

## 실행

현재 superblue1 결과를 사용하려면 `bin/`에서:

```sh
sh ./run_compare_timing_placement.sh
```

기본 출력은 `results/two_db_timing/comparison/`입니다. 이미 존재하면 새 이름을 지정합니다.

```sh
TC_COMPARE=results/two_db_timing/comparison_v2 sh ./run_compare_timing_placement.sh
```

기본 실행은 **geometry + 로그 분석**만 합니다. WNS/TNS를 새로 계산하려면:

```sh
TC_COMPARE=results/two_db_timing/comparison_sta \
sh ./run_compare_timing_placement.sh \
  --sta --rc-r 2.535 --rc-c 1.6e-16 --ignore-net-degree 100 --paths 10
```

이 RC 값은 superblue1 설정입니다. 다른 공정에 그대로 사용하지 마세요.
STA 옵션은 원본 CPU OpenTimer 모델을 세 번 순차 복원해 분석하므로 시간이 걸립니다.
GPU는 사용하지 않습니다. 최신 timing cache가 필요하며, 캐시가 호환되지 않으면
자동 재생성하지 않고 오류를 냅니다. FLUTE LUT 경로 때문에 설치된 `bin/`에서 실행합니다.

## 세 비교 조건

| 이름 | 위치 처리 | 용도 |
|---|---|---|
| A_original | 원본 최종 DEF의 셀 위치 + 방향을 반영한 원본 pin offset | 원본 기준 |
| B_original_centers | 원본 배치를 유지하되 같은 클러스터의 모든 핀을 멤버 셀 중심의 면적 가중 평균 위치로 투영 | 중심 근사의 영향 |
| C_two_db | 축약 최종 DEF에서 클러스터 중심으로 원본 핀을 투영; 비병합 셀은 자기 pin 위치 유지 | two-DB 결과 |

B는 합법적인 클러스터 배치를 만드는 것이 아니라 동일 배치에서의 timing/geometry
근사 실험입니다. B와 C는 모두 내부 핀을 한 점으로 모으지만, cluster 면적 증가,
초기화, 최적화 설정 차이는 여전히 존재합니다. 이 비교만으로 원인의 기여율을
완전히 분리했다고 해석하지 마세요.

HPWL은 원본 모든 net에 대해 **weight 없이 micron으로** 계산합니다. 따라서 두 배치의
서로 다른 net weight 때문에 값이 부풀려지는 문제를 피합니다. HPWL은 Steiner 길이,
RC delay 또는 signoff timing 그 자체는 아닙니다.

`--sta`에서는 A/B/C 모두 같은 원본 binary timing 모델, RC 값, fanout threshold를
사용합니다. original graph의 cell delay/pin cap는 유지하며, 위치만 바꿉니다.
net weight 갱신은 하지 않습니다. 내부 wire RC=0 근사를 포함합니다.
WNS/TNS는 late, ps 단위입니다. 최악 경로 JSON의 arrival/slack은 JSON에 기록된
`time_unit_seconds`를 사용하는 OpenTimer 원래 단위입니다.

## 출력

- `report.md`: 세 경우의 HPWL 합, 셀 면적, 설정 차이, 로그 지표, 선택적 STA 결과.
- `summary.json`: 통계·입력 경로·DEF SHA256·로그 출처를 포함한 상세 데이터.
- `net_comparison.tsv`: 모든 원본 net, degree, 클러스터 연결 여부, A/B/C HPWL,
  C−A, C−B, C/A. **C−A가 큰 순서**이며 동률은 원본 net ID 순서입니다.
- `top_external_nets.tsv`: 외부 연결 net 중 길이 증가 순위 상위 `--top`개(기본 100).
- `top_net_pins.tsv`: 해당 net들의 각 pin 이름/방향, 셀 이름/ID, cluster ID,
  placement node ID, A/B/C pin 좌표. 병합하지 않은 셀의 장거리화도 확인할 수 있습니다.
- `A_original.hpwl.npy`, `B_original_centers.hpwl.npy`, `C_two_db.hpwl.npy`:
  원본 net ID 순서의 binary HPWL 배열.
- `original_timing_progress.tsv`, `placement_timing_progress.tsv`: 로그에서 추출한
  timing 평가 iteration 및 WNS/TNS(ps).
- `*.paths.json`: `--sta` 실행 시 세 경우의 worst late path 보고서.

`C/A`의 A가 0이면 TSV는 빈 칸, JSON 통계의 분모가 0이면 `null`입니다.
pin direction은 저장된 물리 DB의 값이며 electrical driver를 새로 추정하지 않습니다.

## 로그 / 설정 / 입력 검증

기본 래퍼는 `log2`를 원본, `log1`을 two-DB 로그로 사용합니다(파일이 있을 때만).
`TC_ORIGINAL_LOG`, `TC_TWO_DB_LOG`로 변경하거나 직접 CLI 플래그를 사용합니다.
**로그의 실제 parameters가 설정 파일보다 우선합니다.** 로그에 parameters가 없으면
설정 파일은 `provided_config_not_verified_as_run`으로 표시합니다. 최종 수치만 있는
로그에서 feedback 횟수가 보이지 않아도 0이라고 판단하지 않고 `null`로 둡니다.
여러 실행을 이어 붙인 로그는 오류로 처리합니다.

기록된 weighted HPWL, congestion, overflow는 모델·분모·설정이 다를 수 있으므로
자동으로 개선 비율을 주장하지 않습니다. 로그 WNS/TNS 역시 당시 서로 다른 실행의
관측값입니다. 같은 RC 조건에서의 재평가는 `--sta` 결과를 보세요.

DB는 현재 MakeDB의 `physical_db/*.npy` 형식이어야 합니다. mapping checksum과 두
DB의 ID/소유 관계를 검사합니다. DEF의 cell 이름/집합/master, 단위, 배치 상태와
orientation을 검사합니다. PINS는 저장 당시와 동일해야 하며 IO 변경은 지원하지
않습니다. 이 도구는 DEF의 NETS를 다시 가져오지 않고 **저장된 DB 연결 관계를 사용**합니다.
연결 관계를 변경했다면 DB와 mapping을 먼저 다시 만드세요.

다른 설계는 직접 실행할 수 있습니다:

```sh
python3 dreamplace/CompareTimingPlacement.py \
  --original-db ORIGINAL_SAVE --placement-db REDUCED_SAVE --mapping ID_MAPPING \
  --original-def original_final.def --placement-def reduced_final.def \
  --original-log original.log --placement-log reduced.log \
  --output NEW_DIRECTORY
```

원래 배치에서 초기화·bin·feedback 시점을 맞추는 A/B 최적화 실험은 별도로 실행해야
합니다. 이 도구는 입력 결과를 평가하며 기존 실행 설정을 수정하거나 배치를 실행하지 않습니다.
