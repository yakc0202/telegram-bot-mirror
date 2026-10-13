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
- `REQUESTS.md`도 보안 값을 가려서 미러에 올린다. 공개하면 안 되는 항목은 제목 줄에
  `<!-- mirror:skip -->`를 붙이면 그 항목 전체가 빠진다.
- 미러 제외 파일: `SYNC.md`(운영 맥락), `CLAUDE.md`(git 밖).

## 변경 후 배포

1. `python3 -m py_compile main.py`로 문법 확인, 가능하면 실제 데이터로 dry-run.
2. 커밋 후 `git push origin main`. push하면 self-hosted Actions가 봇을 재시작하므로
   **push 전에 사용자 확인을 받는다**. `*.md`만 바뀐 push는 배포하지 않는다.
3. `./mirror-push.sh --push`로 공개 미러에 반영.
4. 위 단계를 하나라도 못 했으면 "완료"라 하지 않고 남은 일을 기록·보고한다.

## 운영 디렉터리

- `~/telegram-bot`은 로컬 git 체크아웃이자 배포 타깃이다. `~/Documents/*` 아래로 옮기지
  않는다(macOS TCC 때문에 LaunchDaemon이 실행되지 않음).
- git에 없는 로컬 전용 파일을 두면 `deploy.yml`의 rsync `--exclude`에도 추가한다
  (`rsync --delete`가 지운다).
- 로컬 `git log`가 비정상적으로 짧으면 `git fetch --unshallow origin main`으로 확인하고,
  복구용 `git reset --hard origin/main`은 사용자 확인 후 실행한다.
- `CLAUDE.md`는 로컬 전용(`.gitignore`)이며 맨 위 `@AGENTS.md`로 이 파일을 불러온다.
