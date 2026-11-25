# 자사주 매입 모니터

상장사 자사주 매입(직접취득·신탁)의 일별 신청·체결 현황을 한 화면에서 보는 대시보드다.
투자 판단은 넣지 않았고, 누가 얼마나 샀고 얼마가 남았는지 파악하는 데 목적을 둔다.

## 자료 출처

| 자료 | 출처 | 스크립트 |
|---|---|---|
| 일별 신청수량·체결수량·체결금액 | KIND 거래소 공시 「자기주식매매 신청내역/체결내역」(공시유형 0326) | `scripts/kind.py` |
| 취득 예정 금액·기간·목적·1일 한도, 결과보고서 | OpenDART 주요사항보고서 구조화 API | `scripts/dart.py` |
| 종가·거래량·거래대금·시가총액 | KRX OPEN API 일별매매정보 | `scripts/krx.py` |

- KIND 체결내역은 직접취득에 한해 누적체결량과 신고수량을 함께 준다. 신탁은 누계가 없어 계약 시작일 이후 체결금액을 직접 더한다.
- 유가증권 문서는 금액이 천원 단위이고 신청수량이 들어 있다. 코스닥 문서는 원 단위이고 신청수량이 없어 전날 신청내역 문서에서 가져온다.
- KIND는 짧은 시간에 요청이 몰리면 429를 준다. 수집기는 요청 간격을 0.6초로 두고, 429를 받으면 기다렸다가 이어서 받는다.

## 실행

```
pip install requests
python run_daily.py          # 최근 10일 갱신 후 docs/data 재생성
python -m http.server 8765 --directory docs   # http://localhost:8765 에서 확인
```

환경변수 `DART_API_KEY`, `KRX_API_KEY` 가 필요하다. 프로젝트 루트에 `.env` 파일을 두어도 된다.

```
DART_API_KEY=...
KRX_API_KEY=...
```

처음 구축할 때는 과거 자료를 한 번 받아야 한다(KIND 13개월치에 1시간 안팎).

```
python scripts/kind.py --from 2025-11-01
python scripts/krx.py  --from 2025-07-01
python scripts/dart.py --days 520
python scripts/dart.py --days 1 --fill-missing
python scripts/build.py
```

`data/` 폴더를 통째로 옮기면 백필을 다시 할 필요가 없다.

## 맥미니로 옮기기

1. 폴더 전체(`data/` 포함)를 맥미니로 복사한다. OneDrive 동기화, AirDrop, `scp`, git 어느 쪽이든 된다.
2. `pip3 install requests` 후 `.env` 에 키 두 개를 적는다.
3. `python3 run_daily.py` 로 한 번 돌려 본다.
4. `deploy/com.buyback.monitor.plist` 의 `__PROJECT__`, `__PYTHON__` 을 실제 경로로 바꿔 `~/Library/LaunchAgents/` 에 넣고 `launchctl load` 한다. 평일 20:40과 다음 날 08:30에 실행된다.

화면을 보는 방법은 둘 중 하나다.

- GitHub Pages: 저장소를 만들고 Pages 소스를 `docs/` 로 지정한 뒤 plist 에 `--push` 를 추가한다. 맥미니가 매일 갱신분을 올린다.
- 맥미니에서 직접 서빙: `python3 -m http.server 8765 --directory docs` 를 상시 띄워 두고 같은 네트워크에서 접속한다.

## 파일 구성

```
scripts/kind.py    KIND 일별 신청·체결 수집 → data/kind_exec.csv, data/kind_orders.csv
scripts/krx.py     전종목 일별 시세 → data/px/YYYYMMDD.csv.gz
scripts/dart.py    자사주 프로그램·결과보고서 → data/dart_programs.json, data/dart_reports.json
scripts/build.py   지표 계산 → docs/data/index.json, docs/data/p/<id>.json
docs/index.html    대시보드(정적 페이지, 외부 라이브러리 없음)
data/holidays.json 앞으로의 휴장일. 남은 거래일 계산에 쓰므로 해마다 보충한다
```

## 알아 둘 점

- 직접취득 진행률은 수량 기준(거래소 누계 ÷ 신고수량)이다. 금액을 정해 놓고 사는 회사는 주가가 오르면 수량을 다 채우지 않고 끝낼 수 있어, 상세 화면에 금액 기준 진행률을 같이 적었다.
- 신탁은 계약금액을 다 쓰지 않아도 되고, 계약 연장·해지가 잦다. 직전 계약 종료 전후 10일 안에 같은 금액으로 다시 체결된 계약은 연장으로 보고 누계를 이어 붙인다.
- 한 회사에 신탁계약이 여러 개 겹치면 먼저 시작한 계약부터 채워지는 것으로 가정한다.
- 수집 시작일 이전에 시작한 신탁계약은 그 이전 체결분이 누계에서 빠진다. 화면에 따로 표시된다.
