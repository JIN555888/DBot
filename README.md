# DBot — 디스코드 스케줄 타이머 봇

채널에 보드 메시지 하나를 띄우고, 봇이 그 메시지만 계속 수정해서 타이머마다 진행률 바와 퍼센트를 갱신한다. 새 메시지는 쌓이지 않는다.

```
⏱ 스케줄 타이머
⏭ 다음 차례 물 마시기 · 2분 후

🟢 회의 🔔                     🔴 물 마시기
███████▍░░░░ 62%               ███████████▏ 93%
⏳ 22:40 남음                   ⏳ 02:04 남음
🔁 매시 정각 · 오후 12:00        🔁 30분마다 · 오후 12:00

2/20개 · 5초마다 갱신 · 11:36:22
```

한 줄에 2개씩, 최대 10줄(20개)까지 격자로 보여 준다. 남은 비율에 따라 점 색이 🟢 → 🟡(70%) → 🔴(90%) 로 바뀐다.

## 1. 봇 만들기 (한 번만)

1. https://discord.com/developers/applications 에서 New Application 을 누른다.
2. Bot 탭에서 Reset Token 을 눌러 토큰을 복사한다.
3. OAuth2 > URL Generator 에서 scopes `bot`, `applications.commands` 를 고른다. 권한은 `Send Messages`, `Embed Links`, `Read Message History`, `View Channels` 를 고른다.
4. 만들어진 URL 로 서버에 초대한다.

## 2. 실행

```powershell
cd C:\Users\jwy\Desktop\Repos\discord-timer
copy .env.example .env      # DISCORD_TOKEN, GUILD_ID 채우기
.\.venv\Scripts\python bot.py
```

venv 가 없으면 먼저 `python -m venv .venv` 와 `.\.venv\Scripts\pip install -r requirements.txt` 를 실행한다.
`GUILD_ID` 를 넣으면 슬래시 명령이 그 서버에 바로 뜬다. 비우면 전역 등록이라 늦게 뜰 수 있다.

## 3. 명령

| 명령 | 설명 |
|---|---|
| `/timer board` | 지금 채널에 보드를 띄운다. 다시 쓰면 예전 보드를 지우고 옮긴다 |
| `/timer add name every [at] [notify]` | 타이머를 추가한다 (최대 `MAX_TIMERS` 개) |
| `/timer remove name` | 타이머를 삭제한다 |
| `/timer reset name` | 타이머를 지금부터 다시 시작한다 |
| `/timer list` | 목록을 나만 보이게 띄운다 |

`every` 는 주기다: `30m`, `1h`, `1h30m`, `90s`, `1d`, `2시간`, `45`(분 단위). 최소 10초.

`at` 은 기준 시각이다:

| 예시 | 결과 |
|---|---|
| (비움) | 지금부터 `every` 마다 |
| `every:1h at:정각` | 매시 00분 |
| `every:1h at::30` | 매시 30분 |
| `every:30m at:정각` | 00분, 30분 |
| `every:1d at:09:00` | 매일 09:00 |
| `every:2h at:정각` | 00, 02, 04 … 시 |

`notify:True` 면 한 바퀴 돌 때마다 채널에 알림을 보내고, `NOTIFY_DELETE_SEC` 초 뒤에 지운다.

## 설정 (.env)

| 키 | 기본 | 설명 |
|---|---|---|
| `UPDATE_SEC` | 5 | 보드 수정 주기(초). 디스코드 속도 제한 때문에 5 이상을 권장한다 |
| `NOTIFY_DELETE_SEC` | 60 | 알림 자동 삭제(초). 0 이면 지우지 않는다 |
| `TZ_HOURS` | 9 | 시간대 |

최대 개수(20개, 2열 x 10줄)와 바 길이(12칸)는 코드에 고정돼 있다.

타이머와 보드 위치는 `state.json` 에 저장한다. 봇을 다시 켜도 같은 메시지를 이어서 수정한다.
