# 텔레그램 주식 정보 중복 제거 봇

여러 텔레그램 채널에 올라오는 주식·투자 메시지 중 완전히 같은 것과 표현만 다르고 내용이 같은 것을
걸러내고, 새 정보만 요약을 붙여 내 채널로 모아 주는 텔레그램 유저봇입니다. 하루 두 번(08시·18시)
금리·에너지 중심의 시장 정리도 보냅니다.

- 원본은 비공개 저장소이고, 공개 미러(`telegram-bot-mirror`)에는 보안 값(토큰·키·채팅방 ID 등)을
  뺀 코드·문서를 `mirror-push.sh`로 동기화합니다. 미러의 배포 워크플로는 `docs/deploy.yml.reference`
  (참고용, 실행 안 됨)입니다.
- 인프라·코드·배포·클라우드 이전·AI 전환 검토: [`docs/봇-구성-안내.md`](docs/봇-구성-안내.md)
- 에이전트 작업 규칙: [`AGENTS.md`](AGENTS.md)

---

## 하는 일

1. **실시간 전달**: 감시 채널(19곳)의 새 메시지를 받아 키워드 규칙 → 중복 판단 → AI 요약 → 요약 채널로 전송.
   일반 요약을 타는 메시지는 잡담(STOCK/CHITCHAT/OPINION) 판단을 요약 호출에 합쳐 메시지당 AI 호출을 줄임
   (`MERGED_LLM_CHECKS`, 기본 켜짐). 잡담으로 판정되면 중복 판단에 등록한 항목을 되돌림.
   브리핑류 전용 요약·짧은 캡션은 잡담 판단을 먼저 따로 호출.
2. **일간 정리**: 08시(밤사이)·18시(아시아 장중) 회차마다 모은 메시지와 시세·금리 데이터를 AI가
   "Daily Market Strategist" 관점으로 정리해 텍스트와 HTML 파일로 전송.
3. **자동 복구**: 재시작하면 채널별 처리 위치부터 꺼져 있던 동안의 메시지를 다시 처리하고, 밀린 정리 회차를
   보냄. 전송에 실패한 미디어는 30초마다 다시 보냄.

## 중복 판단 순서 (`is_duplicate()`)

1. **완전히 같은 글**: 공백·링크를 정리한 텍스트의 해시가 같으면 버림.
2. **같은 링크**: 같은 기사 링크이고 문구도 충분히 비슷하면 버림.
3. **"절대 다른 정보" 필터**: 국가, DART 종목코드, 브리핑 날짜, 국채 만기, 수출 품목, 리포트 담당자,
   해시태그 등이 다르면 유사도 계산 없이 새 정보로 확정.
4. **키워드·숫자 유사도**: `0.7 × 키워드 + 0.3 × 숫자`.
   - `KEYWORD_THRESHOLD_HIGH`(기본 0.6) 이상 → 버림
   - `KEYWORD_THRESHOLD_LOW`(기본 0.25)~HIGH → AI에게 "같은 정보냐" 확인
     (점수 높은 순으로 최대 `MAX_LLM_DUP_CANDIDATES`개)
   - LOW 미만 → 새 정보

대부분은 1~3단계에서 AI 호출 없이 걸러지고, 애매한 경우에만 AI를 씁니다. 중복 확인은 후보마다 따로 묻고
요약 호출에 합치지 않습니다(합치면 새 정보를 중복으로 오판함, 근거 `docs/테스트/`).

## 일간 정리

- **금리축**: 경기·물가 → Fed → 미 국채(명목/실질/BEI) → 달러 → 증시. 10년물 변화가 BEI 주도인지
  실질금리 주도인지 구분.
- **에너지축**: 원유 → 정제마진 → 천연가스·LNG → 전력 → 물가 → 장기금리 → 기업 마진 → 섹터.
- **2027 에너지 강세 가설**은 매일 검증·반증하는 가설로 다루고, 반대 데이터도 같은 비중으로 다룸.
- **정량 데이터**: yfinance(지수·VIX·SOX·DXY·원달러·Brent·WTI·Henry Hub·TTF, 3-2-1 크랙 스프레드
  계산), FRED(10Y·2Y·10Y 실질금리·BEI, 기준일 표기), 18시 회차엔 아시아 지수 추가.
- **작성 AI**: Claude Code → Codex → Gemini 순(아래 "AI 호출"). 문체는 '~습니다'로 통일.
- **보관**: 보낸 정리는 원본 입력과 최종본을 `DIGEST_ARCHIVE_DIR`(기본 `~/.local/state/telegram-bot/digests`)에
  최근 것만 남겨 보관.
- **스케줄러**: 30초마다 실제 시계로 미처리 회차를 확인(맥이 잠들었다 깨도 바로 발송). 다음 회차가
  지난 낡은 회차는 건너뛰고, 실패하면 10분 간격 3회 재시도.

## AI 호출

- `LLM_PROVIDER`(anthropic/openai/gemini)로 1차 제공자를 고르고 `LLM_FALLBACK_PROVIDERS`로 대체 순서 지정.
  `nvidia`(build.nvidia.com NIM, OpenAI 호환)도 대체 제공자로 넣을 수 있음(`NVIDIA_API_KEY`·`NVIDIA_MODEL`,
  분당 `NVIDIA_RPM` 제한). 지금 운영은 대체 제공자를 쓰지 않음.
- 운영은 Gemini. `GEMINI_API_KEYS`의 키를 돌려 쓰며 키별 분당 사용량을 추적하고, 모두 쉬는 중이면
  `GEMINI_MAX_COOLDOWN_WAIT`(기본 5초)까지만 기다린 뒤 "새 정보"로 안전하게 처리. 키는 URL 대신
  `x-goog-api-key` 헤더로 보냄.
- 메시지별 호출은 평일 하루 약 2,000회(중복 확인이 가장 많음). Gemini 실패율이 오른 원인은 로그 집계로
  503(서버 과부하)·429(한도 초과)임을 확인했고, 대체 모델은 같은 메시지로 시험한 뒤 채택하지 않음.
- **08시·18시 정리만** 구독형 CLI로 먼저 작성: Claude Code(`claude -p`) → Codex(`codex exec`) → 둘 다
  실패하면 위 `LLM_PROVIDER`(Gemini). 순서는 `DIGEST_CLI_PROVIDERS`(기본 `claude,codex`). 정리 한 번에
  1~2분 걸림. 서비스(LaunchDaemon)에서는 키체인을 못 읽으므로 `claude setup-token` 토큰을
  `.env`의 `CLAUDE_CODE_OAUTH_TOKEN`에 둠. 메시지별 호출은 횟수가 많아 Gemini 그대로.

## 운영 구조

```
운영 맥 ── LaunchDaemon(com.telegram-bot) ── run_bot.sh ── main.py
   ▲
   └─ self-hosted runner ◄── git push origin main (비공개 저장소)
        rsync → venv 설치 → py_compile → launchctl 재시작
```

- 운영 폴더: `~/telegram-bot` (로컬 git 체크아웃 겸용, `~/Documents` 아래로 옮기지 않음)
- 설정: `~/.config/telegram-bot/.env` (템플릿은 공개 미러의 `.env.example`)
- 미러 동기화: `./mirror-push.sh`(미리보기) → `./mirror-push.sh --push`
- `*.md` 문서만 바뀐 push는 배포·재시작하지 않음
- 자세한 내용과 클라우드(Lightsail) 이전 검토는 [`docs/봇-구성-안내.md`](docs/봇-구성-안내.md)

## 처음 설치

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# ~/.config/telegram-bot/.env 작성 (필수: TELEGRAM_API_ID/HASH, SOURCE_CHATS, DIGEST_CHAT,
# TELEGRAM_BOT_TOKEN, LLM_PROVIDER에 맞는 API 키)
set -a; source ~/.config/telegram-bot/.env; set +a
.venv/bin/python3 main.py
```

- 첫 실행 때 전화번호와 텔레그램 인증 코드를 입력하면 세션 파일이 생겨 이후 자동 로그인됩니다.
- `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`는 https://my.telegram.org → API development tools에서 발급.
- 채널 ID 확인: `.venv/bin/python3 list_chats.py` (로컬 전용 스크립트)

## 보안

- 로그에 봇 토큰·Gemini 키·NVIDIA 키가 남지 않게 로깅 필터(`_RedactSecretsFilter`)로 가림.
- 공개 미러로 내보낼 때 `.env`의 보안 값을 읽어 가리고, 토큰·키·채널 ID 모양이 남으면 push하지 않음.
- 배포 러너 계정은 봇 재시작에 필요한 명령만 sudo로 허용.

## 알아둘 점

- 유저봇은 내 계정으로 자동화합니다. 과도하게 쓰면 계정 제한 위험이 있어 개인용으로만 씁니다.
- 세션 파일은 계정 접근 권한과 같습니다. 복사·공유하지 않습니다.
- 맥이 잠들면 봇도 멈춥니다. 정리는 깨어나는 즉시 나갑니다.
- `main.py` 한 파일(약 4,300줄)이고 자동 테스트는 없습니다. 배포 때 `py_compile` 문법 검사만 합니다.
