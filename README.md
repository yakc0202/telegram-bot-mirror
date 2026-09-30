# Telegram Stock Dedup Bot

여러 텔레그램 채팅방에 올라오는 주식 정보 메시지 중 완전히 동일한 것과 표현만
다르고 내용이 같은 것을 걸러내고, 새로운 정보만 지정한 채널로 모아주는 텔레그램
유저봇입니다.

> **이 저장소에 대해**
> 이 repo는 **비공개 원본의 공개 미러**입니다. 실제 운영 코드는 private repo로
> 유지되고, 그쪽 `main` 브랜치에 push가 들어가면 macOS ARM64 self-hosted runner
> 한 대가 GitHub Actions로 `rsync` 배포 → 의존성 설치 → 문법 체크 →
> `launchctl`로 서비스 재시작까지 수행합니다. 실제 배포에 쓰는 워크플로 파일은
> [`docs/deploy.yml.reference`](docs/deploy.yml.reference)에 참고용으로만
> 넣어뒀고, 이 미러에는 `.github/workflows/`로 올리지 않아 실행되지 않습니다.
> 이 repo는 코드와 문서만 동기화합니다.

---

## 무엇을 하는 코드인가

같은 정보가 여러 텔레그램 리서치/뉴스 채널에 거의 동시에 올라오는 경우가
많습니다(같은 속보, 같은 실적 발표, 같은 기사에 대한 코멘트 등). 이 봇은 감시
중인 채팅방들의 새 메시지를 실시간으로 받아서, 이미 전달한 정보와 같은
내용이면 스킵하고 새 정보만 결과 채널로 forward합니다.

## 중복 판정 파이프라인

`is_duplicate()` 하나에 다 들어있는 로직인데, 단계별로 보면 이렇습니다.

1. **완전 동일 메시지** — 텍스트를 정규화(공백/링크 제거, 소문자화)한 뒤 해시로
   비교. 이미 본 해시면 즉시 스킵.
2. **같은 링크 공유** — 메시지에서 URL을 추출해 최근 기록과 비교. 같은 기사
   링크를 공유하면 문구가 완전히 달라도("또 시작" vs "[속보] ...") 유사도가
   충분히 높은 경우 즉시 중복으로 판단. 링크는 같은데 문구 유사도가 낮으면
   "같은 기사에 대한 다른 의견"일 수 있어 3번으로 넘김.
3. **"절대 다른 정보" 예외 필터** — 키워드/숫자 유사도만 보면 오탐이 나는
   케이스들을 먼저 걸러냅니다. 국가(중국 CPI vs 독일 수출), DART 종목코드,
   브리핑 날짜, 국채 만기, 수출 품목, 리포트 담당자 태그, 해시태그 등 11종의
   속성이 있고, 두 메시지가 형식은 똑같아도 이 중 하나라도 다르면 유사도
   계산 없이 바로 "다른 정보"로 확정합니다. 반복되는 템플릿 리포트(정기
   브리핑, 실적 프레스 등)에서 실제로 겪은 오탐들을 하나씩 막으면서 늘어난
   목록입니다.
4. **키워드/숫자 유사도 → 애매하면 LLM 최종 판단** — 위 필터를 통과한 후보들의
   키워드 자카드 유사도 + 숫자(가격 등) 유사도를 계산합니다.
   - `KEYWORD_THRESHOLD_HIGH`(기본 0.6) 이상이면 LLM 호출 없이 바로 중복 확정
   - `KEYWORD_THRESHOLD_LOW`(기본 0.25)~`HIGH` 사이면 애매한 구간 → LLM에게
     "같은 정보냐" 최종 확인. 점수 높은 순으로 최대
     `MAX_LLM_DUP_CANDIDATES`개까지 순서대로 물어봄
   - `LOW` 미만이면 새 정보로 통과

LLM 호출을 자동 확정 가능한 구간 밖으로 최대한 밀어낸 게 이 설계의 핵심입니다.
대부분의 메시지는 1~3단계에서 API 호출 없이 걸러지고, 진짜 애매한 경우에만
비용을 씁니다.

## LLM 호출: 3사 폴백 + Gemini 키 로테이션

`LLM_PROVIDER`로 1차 provider(anthropic/openai/gemini)를 고르고,
`LLM_FALLBACK_PROVIDERS`로 실패 시 순서대로 넘어갈 provider를 지정할 수
있습니다. Gemini는 무료 티어 rate limit(분당 15회)에 걸리기 쉬워서,
`GEMINI_API_KEYS`에 콤마로 여러 키를 등록하면 요청마다 순환 사용하고, 키별로
남은 쿨다운을 추적해 전부 쿨다운 중이면 `GEMINI_MAX_COOLDOWN_WAIT`만큼만
기다립니다.

## 그 외 기능

- **일간 정리(digest)**: `DAILY_DIGEST_ENABLED=true`면 08시/18시에 금리(명목/
  실질/기대인플레이션), 에너지(원유/정제마진/천연가스), AI·전력수요, 지정학,
  자금흐름을 하나의 매크로 프레임으로 엮은 브리핑을 텍스트+HTML로 보냄
- **잡담 필터**: 주식과 무관한 메시지를 LLM으로 걸러냄 (`CHITCHAT_FILTER_ENABLED`)
- **텔레그램 업로드 용량 제한(413) 자동 폴백**: Bot API로 미디어 전송이 413으로
  실패하면 유저봇 계정으로 대신 전송

## 배포 (원본 private repo 기준)

```yaml
runs-on: [self-hosted, macOS, ARM64, main]
```

`main` 브랜치 push → self-hosted runner가 `rsync`로 배포 → venv 재구성 →
`py_compile`로 문법 체크 → `launchctl bootout/bootstrap/kickstart`로 LaunchDaemon
재시작. 워크플로 전체는 [`docs/deploy.yml.reference`](docs/deploy.yml.reference)
참고 (이 미러에서는 실행되지 않는 참고용 파일입니다).

## 실제로 겪은 장애들 (커밋 히스토리 기준)

- Gemini 키가 전부 쿨다운 상태일 때 `None`을 그대로 `raise`해서 죽던 버그
- 예방적 재연결 로직이 `run_until_disconnected()`를 깨워버려 프로세스가
  `exit 0`으로 조용히 종료되던 버그 (크래시가 아니라서 알아채기 어려웠음)
- 재전송 큐 조회 중 DB 예외가 나면 재전송 루프 태스크 자체가 영구히 죽던 버그
- Bot API 미디어 업로드가 용량 제한(413)으로 실패할 때 아예 전송이 안 되던
  문제 → 유저봇 계정 자동 폴백으로 해결
- 모든 DB 호출을 `asyncio.to_thread`로 위임해 이벤트 루프가 sqlite I/O로
  멈추는 상황 제거

## 알려진 한계

- `main.py` 단일 파일, 4,149줄. 모듈 분리 없음
- 테스트 없음. `py_compile` 문법 체크만 CI에 있음 (배포 워크플로 참고)
- 텔레그램 "유저봇" 방식이라 본인 계정으로 자동화를 수행함 — ToS상 과도한
  자동화는 계정 제한 위험이 있어 개인용으로만 사용 권장
- 이미지만 있고 텍스트가 없는 메시지는 중복 판단 없이 항상 통과됨

---

## 로컬에서 돌려보기

```bash
python -m venv venv
source venv/bin/activate      # Windows는 venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# .env를 열어서 값 채우기 (필수 항목: TELEGRAM_API_ID/HASH, SOURCE_CHATS,
# DIGEST_CHAT, 그리고 LLM_PROVIDER에 맞는 API 키)

python main.py
```

최초 실행 시 전화번호와 텔레그램 인증 코드를 콘솔에 입력하면 세션 파일이
생성되어 이후엔 자동 로그인됩니다.

`TELEGRAM_API_ID` / `TELEGRAM_API_HASH`는 https://my.telegram.org 에서
발급받습니다.
