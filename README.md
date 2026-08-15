# RSI 14 미국주식 알림 봇 (GitHub Actions)

미국 주식 티커들의 일봉 기준 RSI(14)를 주기적으로 계산해서, RSI가 20 이하로 떨어지면 텔레그램으로 알림을 보내는 무료 배치 프로그램입니다. GitHub Actions의 스케줄 기능(cron)을 이용하기 때문에 서버나 PC를 따로 켜둘 필요가 없습니다.

## 동작 방식 요약

- 평일 미국 정규장 시간대(대략 13:00~21:30 UTC)에 30분마다 자동 실행
- `config/tickers.json`에 있는 티커들을 순회하며 RSI(14) 계산 (계산 라이브러리: [TA-Lib](https://ta-lib.org/) — 기술적 분석 지표 계산의 사실상 업계 표준 레퍼런스 구현체)
- RSI < 20 이면 텔레그램 메시지 발송
- 같은 티커는 하루(한국시간 기준)에 한 번만 알림 — 알림 발송 여부는 `state/last_alert.json`에 기록되고, 실행이 끝날 때마다 자동으로 리포지토리에 커밋됩니다.

## 1. 준비하기 전에

- GitHub 계정 (무료)
- 텔레그램 앱

## 2. GitHub 리포지토리 만들기

1. GitHub에서 새 리포지토리를 만듭니다 (Private 권장 — 어차피 코드에 민감 정보는 없지만 개인 프로젝트니 Private 추천).
2. 이 폴더(`rsi-alert/`) 안의 파일들을 그대로 그 리포지토리에 올립니다 (웹에서 드래그 앤 드롭으로 업로드해도 되고, git으로 push해도 됩니다).

## 3. 텔레그램 봇 만들기

1. 텔레그램에서 **@BotFather**를 검색해서 대화를 시작합니다.
2. `/newbot` 명령을 보내고 안내에 따라 봇 이름을 정합니다.
3. 완료되면 `123456789:AAExampleTokenString...` 같은 형태의 **봇 토큰**을 줍니다. 이걸 복사해둡니다. (이것이 `TELEGRAM_BOT_TOKEN`)
4. 만든 봇과 대화방을 열고 아무 메시지나 하나 보냅니다 (예: "hi"). *봇이 나에게 먼저 말을 걸 수는 없어서, 내가 먼저 말을 걸어야 합니다.*
5. 브라우저에서 아래 주소를 열어봅니다 (토큰 부분을 내 토큰으로 교체):
   ```
   https://api.telegram.org/bot<내토큰>/getUpdates
   ```
6. 결과 JSON에서 `"chat":{"id":123456789, ...}` 형태의 숫자를 찾습니다. 이게 **채팅 ID**입니다. (이것이 `TELEGRAM_CHAT_ID`)
   - 위 방법이 안 되면 텔레그램에서 **@userinfobot**과 대화해서 자신의 chat id를 바로 받을 수도 있습니다.

## 4. GitHub에 시크릿(비밀값) 등록하기

리포지토리 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

다음 두 개를 등록합니다.

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 3번에서 받은 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 3번에서 받은 채팅 ID |

## 5. 워크플로우 권한 확인 (중요)

state 파일을 자동 커밋하려면 Actions가 리포지토리에 쓰기 권한이 있어야 합니다. 워크플로우 파일에 이미 `permissions: contents: write`가 설정되어 있지만, 리포지토리 설정에 따라 막혀있을 수도 있으니 확인해주세요.

리포지토리 → **Settings** → **Actions** → **General** → 아래로 스크롤 → **Workflow permissions** → **Read and write permissions** 선택 → Save

## 6. 테스트 실행

1. 리포지토리 → **Actions** 탭 → 왼쪽에서 **RSI Alert** 워크플로우 선택
2. **Run workflow** 버튼으로 수동 실행
3. 실행 로그에서 각 티커의 RSI 값과 "신규 알림 발송" 여부를 확인할 수 있습니다.
4. RSI가 20 이하인 티커가 있다면 텔레그램으로 바로 메시지가 옵니다. (없다면 조건이 안 걸린 것뿐이니 정상입니다 — `config/tickers.json`에 아무 티커나 넣고 실제로 알림이 오는지 보고 싶으면, `rsi_monitor.py`의 `RSI_THRESHOLD = 20`을 일시적으로 `RSI_THRESHOLD = 90` 같은 값으로 바꿔서 테스트 실행해본 뒤 다시 20으로 되돌리면 됩니다.)

## 7. 감시 티커 추가/삭제하기

`config/tickers.json` 파일만 고치면 됩니다. **코드 수정이나 워크플로우 재실행 설정 없이, 다음 스케줄 실행부터 자동으로 반영됩니다.**

```json
{
  "tickers": ["AAPL", "TSLA", "NVDA", "MSFT"]
}
```

GitHub 웹에서 이 파일을 열고 연필(수정) 아이콘을 눌러 편집 후 커밋하면 끝입니다.

## 파일 구조

```
rsi-alert/
├── .github/workflows/rsi_check.yml   # 스케줄 정의 (cron)
├── config/tickers.json               # 감시할 티커 목록 (여기만 수정하면 됨)
├── state/last_alert.json             # 오늘 이미 알림 보낸 티커 기록 (자동 갱신)
├── rsi_monitor.py                    # 실제 RSI 계산 + 알림 로직
├── requirements.txt
└── README.md
```

## 참고 / 주의사항

- **RSI 계산 기준**: 일봉(daily close) 기준 14일 RSI이며, Wilder의 원래 정의를 그대로 구현한 TA-Lib을 사용합니다. 장중에 실행되면 야후 파이낸스가 그날의 "진행 중인" 종가를 제공하는 경우가 많아 실시간에 가깝게 반영되지만, 완전히 확정된 값은 장마감 후입니다.
- **알림은 티커당 하루 1번**: 알림을 받은 뒤에는 직접 앱에서 확인하는 걸 전제로 설계되어 있어서, 같은 날 RSI가 계속 20 이하여도 재알림은 가지 않습니다. 다음날(한국시간 기준 자정 이후) 초기화됩니다.
- **GitHub Actions 무료 한도**: Private 리포지토리는 월 2000분(Linux 기준)까지 무료입니다. 이 정도 스케줄(평일 하루 18회, 회당 1분 내외)이면 월 100~200분 정도로 넉넉합니다.
- **스케줄 정확도**: GitHub Actions의 `schedule` cron은 트래픽이 몰리면 몇 분 정도 지연될 수 있습니다(공식적으로 안내된 제약). 초 단위로 정확한 감시가 필요한 용도에는 맞지 않습니다.
- **60일 비활성 자동 비활성화 대응**: GitHub은 리포지토리에 60일간 활동이 없으면 예약 워크플로우를 자동으로 꺼버립니다. 이 스크립트는 알림 발생 여부와 무관하게 매 실행마다 `state/last_alert.json`의 `last_checked_utc` 값을 갱신해서 커밋하도록 만들어져 있어, 이 문제를 자동으로 피해갑니다.
- **텔레그램 대신 다른 채널로 바꾸고 싶다면**: `rsi_monitor.py`의 `send_telegram()` 함수만 교체하면 됩니다 (예: 이메일 SMTP, Discord webhook 등).
