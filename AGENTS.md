# 텔레그램 주식정보 중복제거 봇 — 작업 규칙

Claude Code·Codex 등 이 저장소에서 작업하는 모든 에이전트에 적용한다.

## 세션 시작·종료

- 시작하면 [`SYNC.md`](SYNC.md)(지금 상태 요약)를 먼저 읽는다.
- 작업을 마치면 `SYNC.md`를 갱신한다. 지금 상태만 짧게 두고, 끝난 일은 지운다.

## 요청 기록

- 사용자의 모든 요청(질문·버그 신고·기능 요청)은 즉시 [`REQUESTS.md`](REQUESTS.md)에
  추가하고, 처리가 끝나면 상태를 갱신한다. 형식은 `REQUESTS.md` 상단 규칙을 따른다.
- 다른 세션이 기록만 보고 이어서 일할 수 있게 다음을 빠짐없이 적는다:
  - 바뀐 것: 파일·함수·환경변수·채널 ID 등.
  - 알아둘 점: 사용자에게 말한 주의·다음 할 일 전부.
  - 사용자가 해야 하는 일과 그 결과.
  - 막힌 것: 못 한 단계, 이유, 풀 방법. 막혔으면 "완료"로 적지 않는다.
  - 진단 결과: 원인을 찾거나 배제한 근거(로그, 테스트).

## 지침 파일 작성 (agent-doc-guard 원칙 적용)

- `AGENTS.md`·`CLAUDE.md`에 피드백을 반영할 때는 규칙 한 줄로 쓴다.
- 날짜·사건 경위·"예전엔 ~였다" 같은 설명은 넣지 않는다. 경위는 `REQUESTS.md`에, 지금
  상태는 `SYNC.md`에 둔다.

## 공개 미러

- 무엇이든 바꾸면 `telegram-bot-mirror`(공개)에도 반영한다: `./mirror-push.sh`로 미리보기 →
  `./mirror-push.sh --push`.
- 보안 값은 미러에 내보내지 않는다: 정리 채팅방 2개(`DIGEST_CHAT`, `SUMMARY_CHAT`)·로그
  채팅방 2개(`LOG_CHAT_INFO`, `LOG_CHAT_WARNING`) ID와 `.env`에 들어가는 모든 값(토큰·키·
  해시·전화번호·감시 채널).
- `mirror-push.sh`는 `.env`에서 그 값을 읽어 가리고, 보안 값 모양이 남으면 push하지 않는다.
  새 보안 값은 `.env`에 두거나 스크립트 검사 패턴에 추가한다.
- `.env`에 없는 사람 이름·채널 제목은 `~/.config/telegram-bot/mirror-deny.txt`(저장소 밖, 한 줄에 하나)에 두면 `mirror-push.sh`가 `<비공개>`로 가린다.
- `mirror-push.sh`는 파일 내용·파일 이름·커밋 메시지를 검사하고, 멈출 때 값 대신 파일·줄·종류만 찍는다.
- `REQUESTS.md`도 보안 값을 가려서 미러에 올린다. 공개하면 안 되는 항목은 제목 줄에
  `<!-- mirror:skip -->`를 붙이면 그 항목 전체가 빠진다. 표시는 백틱 없이 쓴다(백틱 안은 설명으로 보고 빼지 않는다).
- 미러 제외 파일: `SYNC.md`(운영 맥락).
- `mirror-push.sh`는 커밋된 HEAD만 내보낸다. `.env.example`은 미러에만 있으므로 `mirror-push.sh` 뒤에 미러 저장소에서 직접 커밋한다.

## 변경 후 배포

1. `python3 -m py_compile main.py`로 문법 확인, 가능하면 실제 데이터로 dry-run.
2. 커밋 후 `git push origin main`. push하면 self-hosted Actions가 봇을 재시작하므로
   **push 전에 사용자 확인을 받는다**. `*.md`만 바뀐 push는 배포하지 않는다.
3. `./mirror-push.sh --push`로 공개 미러에 반영.
4. 위 단계를 하나라도 못 했으면 "완료"라 하지 않고 남은 일을 기록·보고한다.

## 시험·확인

- 오래 걸리는 시험은 2~3건으로 먼저 돌려 결과 모양을 확인한 뒤 전체를 돌린다.
- 시험용으로 함수를 바꿔 끼울 때는 원래 함수와 같은 인자 이름·기본값을 유지한다(호출부가 키워드 인자로 부른다).
- 시험 중 Gemini 키 한도는 시험 프로세스도 따로 센다. 호출 간격을 두고 실패분은 다시 돌려 실패 건수와 함께 보고한다.
- 비율·건수 추정은 말하기 전에 실제 로그로 센다. 메시지 구성은 날마다 다르므로 여러 날을 본다.
- 배포 후 로그는 재시작 줄 이후만 센다. 시각이 안 찍힌 줄(Traceback 등)이 있어 시각 문자열로 자르지 않는다.
- 로그 감시 파이프는 모든 단계가 줄 단위로 바로 내보내게 한다(`grep --line-buffered`, `cut`·`awk`는 빼거나 `fflush()`). 감시가 조용하면 로그를 직접 열어 확인한다.

## 운영 디렉터리

- `~/telegram-bot`은 로컬 git 체크아웃이자 배포 타깃이다. `~/Documents/*` 아래로 옮기지
  않는다(macOS TCC 때문에 LaunchDaemon이 실행되지 않음).
- git에 없는 로컬 전용 파일을 두면 `deploy.yml`의 rsync `--exclude`에도 추가한다
  (`rsync --delete`가 지운다).
- 로컬 `git log`가 비정상적으로 짧으면 `git fetch --unshallow origin main`으로 확인하고,
  복구용 `git reset --hard origin/main`은 사용자 확인 후 실행한다.
- `CLAUDE.md`는 `@AGENTS.md` 한 줄만 둔다. 규칙과 개발 맥락은 모두 이 파일에 쓴다
  (Claude Code·Codex 공통).

## 개발 맥락

### 개요

- Telethon 유저봇이 `SOURCE_CHATS`를 감시해 주식·투자 정보를 중복 제거하고 요약해
  요약 채널(`DIGEST_CHAT`)로 보낸다. 08시/18시 정량 데이터 + 금리·에너지 매크로 정리는 정리 채널
  (`SUMMARY_CHAT`)로 보낸다.
- 코드: `main.py`. 실행: LaunchDaemon `/Library/LaunchDaemons/com.telegram-bot.plist` →
  `run_bot.sh`. 배포: push → self-hosted Actions(`.github/workflows/deploy.yml`).
- 흐름: `handler()` → 앨범(grouped_id) 2초 디바운스 → `process_event()` → 잡담 필터
  (STOCK/CHITCHAT/OPINION) → 중복 판단 → 요약 → 전송. 미디어 전송 실패는 30초마다 재시도.
  스티커·투표는 알림 없이 무시.

### 중복 판단

- 순서: 해시 → `similarity_score()`(0.7×키워드 + 0.3×숫자) → 애매하면 LLM
  (`ask_llm_same_info`).
- 새 오탐을 고치기 전에 `extract_*`, `STOPWORDS`, LLM 프롬프트에 비슷한 처리가 있는지 먼저
  본다. 같은 유형이 채널마다 다른 모양으로 반복된다.
- 숫자 비교: `NUMBER_MATCH_TOLERANCE` 0.1%. 1900~2100(연도)와 소수점 없는 1~31
  (`_looks_like_calendar_number`)은 제외. 키워드 점수 < `MIN_KW_SCORE_FOR_NUMBER_BOOST`(0.15)면
  숫자 기여 무시.
- 반복 보일러플레이트(증권사 앱 안내, 세션 시점 단어 등)는 `STOPWORDS`에 추가한다.
- 중복 확인은 후보마다 따로 묻고, 요약·잡담 판단 호출에 합치지 않는다(여러 후보를 한 번에 묻거나
  합치면 새 정보를 중복으로 오판함, 근거 `docs/테스트/중복확인-합치기-C4-테스트.md`).
- 정형 표·반복 데이터는 구분 키를 추출해 다르면 제외: `extract_bond_table_type()`(국채 만기),
  `extract_export_data_product()`(수출 품목).
- LLM 프롬프트 원칙: 핵심 주체가 다르면 DIFFERENT(최우선). 우연히 같은 작은 정수는 근거가
  아니다. 식별 정보(시각·수치) 없는 정형 알림은 DIFFERENT. 배경설명 추가는 SAME, 새 수치
  전망(PER·연도별 이익 등) 추가는 DIFFERENT.

### 메시지 포맷

- 인용블럭(`<blockquote>`) 사용, 코드블록 아님.
- 4096자 초과는 `(1/N)` 형식으로 분할 전송(캡션 포함 번호 통일).
- 실제 첨부 사진 판정은 `has_real_photo()`(`MessageMediaPhoto`)로 한다. 링크 미리보기는 사진이
  아니다.
- 정리의 섹션 간 빈 줄은 코드(`rebuilt_blocks`)가 재구성한다.

### 08시/18시 정리 (`_run_digest`)

- 역할: "Daily Market Strategist". 금리축(경기·물가 → Fed → 명목/실질/BEI → 달러 → 증시)과
  에너지축(원유 → 정제마진 → 가스·LNG → 전력 → 물가 → BEI → 장기금리 → 마진 → 섹터)을 연결
  해석한다. 10년물 변화는 BEI 주도인지 실질금리 주도인지 구분한다.
- 2027 에너지 강세는 매일 검증·반증할 가설이다. 반대 데이터도 같은 비중으로 다룬다.
- 디커플링 점검: 유가가 크게 움직였는데 금리가 무반응이면 원인(지표 대기/지정학 노이즈)을
  구분하고 대신 집중하는 지표를 짚는다(🔗 섹션).
- `digest_time`: `morning`(08시)과 `evening`(18시)은 다른 포맷. 조건부 섹션(AI·전력 Chain /
  시장 괴리 체크 / 2차 인플레이션 Watch)은 이슈가 없으면 제목째 생략.
- `classify_digest_blocks`/`render_digest_html`은 제목 문자열이 아니라 이모지 프리픽스
  (🌡️⚡🔗🔀🌐🌾🌙➡️)로 섹션을 분류한다. 새 섹션 이모지를 추가하면 `DIGEST_SECTION_HEADERS`와
  `DIGEST_STRATEGY_PREFIXES` 둘 다에 넣는다.
- PART 2 안의 '- ' 줄은 섹션 본문이다(새 이슈 블록으로 쪼개지 않음).
- `claude -p`는 `--system-prompt`(`DIGEST_CLI_SYSTEM_PROMPT`)로 기본 코딩 도우미 프롬프트를 대체한다.
- 실적 발표/리뷰 섹션은 없다. 필요하면 git 히스토리에서 복원.
- 정량: `fetch_market_quant_data()`(yfinance: 지수·VIX·SOX·DXY·KRW·Brent·WTI·NG·TTF·RB·HO),
  `fetch_rates_quant_data()`(FRED `fredgraph.csv`: DGS10·DGS2·DFII10·T10YIE, 값마다 기준일
  표기), `compute_crack_spread_321()`(RB·HO는 ×42, 원자재 값은 노출 안 함),
  `fetch_asia_quant_data()`(evening만).
- 자동 소스가 없는 지표(JKM, EU 가스저장률, OPEC+ 여유생산 등)는 텔레그램 언급이 있을 때만
  다루고 수치를 만들지 않는다.
- 작성 AI: `call_digest_llm` — `DIGEST_CLI_PROVIDERS`(기본 `claude,codex`) 순으로 `claude -p`/
  `codex exec`를 시도하고 전부 실패하면 `call_llm`(Gemini). 서비스에서 Claude는 `.env`의
  `CLAUDE_CODE_OAUTH_TOKEN`으로 로그인한다. 메시지별 호출은 `call_llm` 그대로.
- 발송한 정리의 LLM 원문·최종본은 `DIGEST_ARCHIVE_DIR`(기본 `~/.local/state/telegram-bot/digests/`, 최근
  60회차)에 남는다. 정리가 이상하다는 피드백은 이 파일부터 본다.
- 텍스트와 HTML(`render_digest_html`, `오전/오후_정리_YYYYMMDD.html`)을 같이 보낸다.

### 정리 스케줄러 (`_digest_loop`)

- 긴 `asyncio.sleep`으로 예약 시각을 기다리지 않는다. macOS 잠자기 중에는 그 시계가 멈춘다.
  `DIGEST_POLL_SECONDS`(30초)마다 실제 시계로 미처리 회차를 확인한다(재시작 catch-up 포함).
- `digest_sent_state.json`에는 처리한 회차 시각을 기록한다(스킵 포함). 텍스트 전송 직후,
  HTML 전송 전에 기록한다. 원자적 쓰기, SQLite와 분리.
- 구간·헤더·파일명 날짜는 회차 기준(`_kst_slot_bounds`)으로 잡는다. '지금' 기준이 아니다.
- 다음 회차 시각이 이미 지난 낡은 회차는 보내지 않고 기록만 한다.
- 실패 시 `DIGEST_RETRY_INTERVAL`(10분) 간격 `DIGEST_MAX_ATTEMPTS`(3회).
- 맥이 잠들면 봇도 멈춘다. 정리는 깨어나는 즉시 나간다.

### 메시지 catch-up (`_catch_up_missed_messages`)

- 채널별 `last_processed_id` 워터마크. 채널 ID는 `client.get_peer_id(entity)`로 정규화한다
  (`-100` 접두어, `event.chat_id`와 일치).
- 앨범은 마지막 장 ID까지 워터마크에 저장한다.
- 워터마크 없는 채널은 현재 시점부터 추적한다.
- 채널 ID가 의심되면 `list_chats.py`로 실제 타이틀을 확인:
  `set -a; source ~/.config/telegram-bot/.env; set +a; .venv/bin/python3 list_chats.py`

### Gemini 속도 제한

- `GEMINI_API_KEYS` 라운드로빈(가용성 폴백 목적), 키별 1분 사용량 추적(`GEMINI_PER_KEY_RPM`, 기본 12).
- 429를 받으면 `_mark_gemini_key_exhausted`.
- `is_duplicate()`는 `llm_processing_lock`을 쥐고 실행되므로 대기는
  `GEMINI_MAX_COOLDOWN_WAIT`(기본 5초)까지만. 넘으면 실패 처리(→ "다른 정보").

### 텔레그램 FloodWait

- 유저봇 `FloodWaitError`·봇 API 429(`retry_after`)는 `_flood_until`에 대기 종료 시각을 적고, 그 전에는 재전송 큐·읽음 처리를 건너뛴다(재전송 큐 시도 횟수도 안 올림).
- 시작 단계(`get_dialogs`·`get_entity`)는 `_call_with_flood_wait(max_wait=None)`으로 끝까지 기다린다. 새 Telethon 호출을 넣으면 같은 래퍼를 쓴다.
- 봇 API 전송 함수는 이벤트 루프에서 동기로 불리므로 안에서 sleep하지 않는다. 재전송 큐가 없는 전송(정리)은 `_send_bot_with_flood_retry`로 감싼다.
- 감시 채널 엔티티는 `_event_chat(event)`(시작 때 만든 표)로 얻는다. 메시지마다 `event.get_chat()`을 부르지 않는다.
- Telethon이 업데이트 따라잡기를 FloodWait로 포기하면 `_gap_recovery_loop`가 그 구간을 `processed_message_ids` 기준으로 다시 훑는다.
- 튜닝: `FLOOD_SLEEP_THRESHOLD`(60, Telethon 자동 대기 한도), `FLOOD_WAIT_MAX_SECONDS`(900), `UPDATE_GAP_RECOVERY_DELAY`(600).

### SQLite

- `text_factory`는 `errors="replace"`.
- `get_db()`에서 `PRAGMA integrity_check`, 손상 시 `.corrupted-YYYYMMDDHHMMSS`로 백업 후 새 DB.
- `set_state`/`get_state`/`save_entry_to_db`는 런타임 손상 시 재연결 후 재시도
  (`_invalidate_db_connection_on_corruption`).

### 운영 (macOS)

- LaunchDaemon(`UserName` 지정)으로 실행한다. LaunchAgent는 GUI 세션에 묶인다.
- 이 환경의 Bash·`!`에서는 `sudo` 비밀번호를 못 받는다. plist 수정·launchctl은
  `osascript -e 'do shell script "..." with administrator privileges'`로 한다.
- sudoers는 러너 계정에 `launchctl bootout/bootstrap/kickstart`(system 도메인)만 NOPASSWD.
- 크래시 리포터 대화상자 끄기: `defaults write com.apple.CrashReporter DialogType none`.
- `run_bot.sh`(git 밖)는 `.venv/bin/python3`를 쓴다. venv가 없으면 종료, 알림 쿨다운 60초.
- 러너 체크아웃: `~/github-action-runners-1/_work/telegram-bot/telegram-bot/telegram` →
  rsync `--delete`로 `~/telegram-bot`. `.git`·로컬 전용 파일은 `--exclude`.
- 환경변수: `~/.config/telegram-bot/.env`(템플릿은 공개 미러의 `.env.example`). 주요 튜닝 값:
  `DEDUP_WINDOW_MINUTES`, `THRESHOLD_HIGH/LOW`, `MIN_KEYWORDS_FOR_AUTO_DUP`,
  `MAX_LLM_DUP_CANDIDATES`, `MAX_AUTO_DUP_SIZE_RATIO`, `NUMBER_MATCH_TOLERANCE`,
  `GEMINI_PER_KEY_RPM`, `GEMINI_MAX_COOLDOWN_WAIT`, `LLM_MAX_CALLS_PER_WINDOW`,
  `DAILY_DIGEST_HOUR`(18), `NIGHT_DIGEST_END_HOUR`(8), `DB_PATH`, `SESSION_PATH`,
  `DIGEST_SENT_STATE_PATH`.
- 로그 출력은 모든 root 핸들러에 단 `_RedactSecretsFilter`가 토큰·키를 가린다. 새 핸들러를 추가하면 이 필터도 단다.
- API 키는 URL 쿼리가 아니라 헤더로 보낸다(Gemini는 `x-goog-api-key`).

### 작업 방식

- 코드 수정 후 `python3 -m py_compile main.py` + 실제 재현 데이터로 테스트.
