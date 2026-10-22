#!/bin/sh
# 공개 미러(telegram-bot-mirror)로 이 저장소의 추적 파일을 내보낸다. 보안 값은 내보내지 않는다.
#   ./mirror-push.sh           미리보기: 바뀔 파일·검사 결과만 보여주고 되돌림(push 안 함)
#   ./mirror-push.sh --push    실제 커밋·push(일반 push, force-push 아님)
# 가릴 값은 ~/.config/telegram-bot/.env 에서 직접 읽는다(토큰·키·해시·전화번호·채팅방 ID).
# .env 에 없는 사람 이름·채널 제목은 ~/.config/telegram-bot/mirror-deny.txt(한 줄에 하나, 없어도 됨)에 두면 같이 가린다.
# 값 목록 파일을 저장소 밖에만 두므로 실제 값은 어떤 저장소에도 커밋되지 않는다.
# 파일 내용·파일 이름·커밋 메시지를 가린 뒤에도 보안 값 모양이 남아 있으면 커밋·push 없이 멈춘다.
# 멈출 때는 값을 찍지 않고 파일·줄·종류만 찍는다(터미널 출력이 AI 대화 기록에 남음).
# 작업 트리가 아니라 커밋된 HEAD 내용을 내보낸다(커밋 안 한 수정은 미러에 안 나감).
# .env.example 은 미러에만 있으므로 고칠 때는 미러 저장소에서 직접 커밋한다(이 스크립트가 미러 작업본을 초기화함).
set -eu
MODE=${1:---check}
case "$MODE" in --check|--push) ;; *) echo "알 수 없는 옵션: $MODE"; exit 1 ;; esac
SRC=$(cd "$(dirname "$0")" && git rev-parse --show-toplevel)
MIRROR=${MIRROR_DIR:-"$HOME/.cache/telegram-bot-mirror"}
ENV_FILE=${ENV_FILE:-"$HOME/.config/telegram-bot/.env"}
DENY_FILE=${DENY_FILE:-"$HOME/.config/telegram-bot/mirror-deny.txt"}
[ -f "$ENV_FILE" ] || { echo "중단: $ENV_FILE 가 없음 (가릴 값을 읽을 수 없음)"; exit 1; }
[ -d "$MIRROR/.git" ] || git clone -q https://github.com/yakc0202/telegram-bot-mirror "$MIRROR"
git -C "$MIRROR" fetch -q origin
git -C "$MIRROR" checkout -q -f main
git -C "$MIRROR" reset -q --hard origin/main
git -C "$MIRROR" clean -fdq
restore() { git -C "$MIRROR" checkout -q -f main; git -C "$MIRROR" reset -q --hard origin/main; git -C "$MIRROR" clean -fdq; }
MSG_FILE="$MIRROR/.git/MIRROR_COMMIT_MSG"  # 가리고 검사한 커밋 메시지

python3 - "$SRC" "$MIRROR" "$ENV_FILE" "$DENY_FILE" "$MSG_FILE" <<'PY' || { restore; exit 1; }
import re, subprocess, sys, unicodedata
from pathlib import Path
src, mirror, env_file, deny_file, msg_file = map(Path, sys.argv[1:])

# 공개하지 않는 파일
EXCLUDE = {
    'SYNC.md',                            # 지금 상태 요약(운영 맥락)
    'docs/테스트/메시지-요약-테스트.md',  # 감시 채널 원문·채널 이름이 들어 있음
    'docs/테스트/중복확인-합치기-C4-테스트.md',  # 감시 채널 원문이 들어 있음
}
# 경로를 바꿔 내보내는 파일: 미러에서 워크플로가 실행되지 않게 참고용으로만 둔다
RENAME = {'.github/workflows/deploy.yml': 'docs/deploy.yml.reference'}
# 미러에만 있는 파일(지우지 않음)
MIRROR_ONLY = {'.env.example'}

# .env 에서 보안 값 수집: 이름에 TOKEN/KEY/HASH/API_ID/PHONE/CHAT 이 들어간 항목
# 5자 미만 값은 가리지 않는다(숫자·단어 일부까지 바뀌어 코드가 깨짐). 건너뛴 이름만 알린다.
SECRET_NAME = re.compile(r'(TOKEN|KEY|HASH|API_ID|PHONE|CHAT)')
pairs, short = [], set()  # pairs: (실제 값, 가림 표시, 멈출 때 찍을 종류)
for line in env_file.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    name, value = (x.strip() for x in line.split('=', 1))
    name = name.removeprefix('export ').strip()
    if not SECRET_NAME.search(name):
        continue
    for v in value.strip('"\'').split(','):
        v = v.strip().strip('"\'')
        if len(v) >= 5:
            pairs.append((v, f'<{name}>', name))
        elif v:
            short.add(name)
# 금지어 목록(저장소 밖, 없어도 됨): 사람 이름·채널 제목처럼 .env 에 없는 값. 2자 이상만
deny = 0
if deny_file.is_file():
    for i, line in enumerate(deny_file.read_text(encoding='utf-8').splitlines(), 1):
        v = line.strip()
        if len(v) >= 2 and not v.startswith('#'):
            pairs.append((v, '<비공개>', f'금지어 {i}번째 줄'))
            deny += 1
pairs.sort(key=lambda p: -len(p[0]))  # 긴 값부터 가림
pairs.append((str(Path.home()), '~', '홈 경로'))  # 맥 계정 이름이 든 홈 경로

# REQUESTS.md 항목 제외 표시. 백틱 안(`<!-- mirror:skip -->`)은 규칙 설명이라 표시로 보지 않는다
SKIP_MARK = re.compile(r'(?<!`)<!-- mirror:skip -->(?!`)')


def public_requests(text):
    # REQUESTS.md: 표시가 붙은 줄을 빼고, 제목 줄(#)에 붙어 있으면 다음 제목 전까지
    # 그 항목 전체를 뺀다
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        marked = bool(SKIP_MARK.search(line))
        if line.startswith('#'):
            skip = marked
        if skip or marked:
            continue
        out.append(line)
    return ''.join(out)

generic = [
    ('텔레그램 봇 토큰', r'\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b'),
    ('Google API 키', r'\bAIza[0-9A-Za-z_-]{35}\b'),
    ('Anthropic/OpenAI 키', r'\bsk-(ant-|proj-)?[A-Za-z0-9_-]{20,}'),
    ('NVIDIA 키', r'\bnvapi-[A-Za-z0-9_-]{20,}'),
    ('GitHub 토큰', r'\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b'),
    ('GitHub 토큰', r'\bgithub_pat_[A-Za-z0-9_]{22,}'),
    ('AWS 액세스 키', r'\b(AKIA|ASIA)[0-9A-Z]{16}\b'),
    ('Slack 토큰', r'\bxox[baprs]-[A-Za-z0-9-]{10,}'),
    ('텔레그램 API hash 모양', r'\b[0-9a-f]{32}\b'),
    ('전화번호', r'\+82[0-9 -]{8,}'),
    ('전화번호', r'(?<![0-9])01[016789][ .-]?[0-9]{3,4}[ .-]?[0-9]{4}(?![0-9])'),
    ('채널·그룹 ID', r'-100[0-9]{9,}'),
    ('개인 키', r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
]
ALLOW = {'-1001234567890'}  # 코드 주석의 예시 ID


def mask(text):
    # .env 값·금지어·홈 경로를 가린다. (가린 문자열, 가린 곳 수)
    n = 0
    for real, m, _ in pairs:
        if real in text:
            n += text.count(real)
            text = text.replace(real, m)
    return text, n


def find_leaks(text):
    # 가린 뒤 남은 보안 값 모양. 값은 돌려주지 않고 (줄 번호, 종류)만
    hits = [(m.start(), kind) for kind, pat in generic for m in re.finditer(pat, text)
            if m.group(0) not in ALLOW]
    hits += [(text.find(real), kind) for real, _, kind in pairs if real in text]
    return [(text.count('\n', 0, i) + 1, kind) for i, kind in hits]


def shown(path):
    # 멈출 때 찍는 파일 이름: 파일 이름에 든 값도 가린다
    path = mask(path)[0]
    for _, pat in generic:
        path = re.sub(pat, '<…>', path)
    return path

# 커밋된 HEAD 기준 파일 목록과 실행 권한
modes = {}
for row in subprocess.run(['git', 'ls-tree', '-r', '-z', 'HEAD'], cwd=src, capture_output=True,
                          check=True).stdout.decode().split('\0'):
    if row:
        meta, f = row.split('\t', 1)
        modes[f] = meta.split()[0]
tracked = [f for f in modes if unicodedata.normalize('NFC', f) not in EXCLUDE]
targets = {f: RENAME.get(f, f) for f in tracked}
keep = set(targets.values()) | MIRROR_ONLY
for p in list(mirror.rglob('*')):
    rel = p.relative_to(mirror)
    if rel.parts[0] != '.git' and p.is_file() and str(rel) not in keep:
        p.unlink()

leaks, masked = [], 0
for name, out in targets.items():
    where = shown(out)
    # 파일 이름은 가리지 않고 검사만 한다(걸리면 EXCLUDE·RENAME 으로 처리)
    leaks += [f'파일 이름 {where}: {kind}' for _, kind in find_leaks(out)]
    dst = mirror / out
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(subprocess.run(['git', 'show', f'HEAD:{name}'], cwd=src, capture_output=True,
                                   check=True).stdout)
    dst.chmod(0o755 if modes[name] == '100755' else 0o644)
    if name == 'REQUESTS.md':
        dst.write_text(public_requests(dst.read_text(encoding='utf-8')), encoding='utf-8')
    data = dst.read_bytes()
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        text = None
    if text is not None:
        text, n = mask(text)
        masked += n
        if text.encode('utf-8') != data:
            dst.write_text(text)
        data = text.encode('utf-8')
        leaks += [f'{where}:{line}: {kind}' for line, kind in find_leaks(text)]
    else:
        leaks += [f'{where}: {kind} (바이너리)' for real, _, kind in pairs if real.encode('utf-8') in data]

# 커밋 메시지: 원본 커밋 제목을 같은 방식으로 가리고 검사한다
msg, n = mask(subprocess.run(['git', 'log', '-1', '--format=%s'], cwd=src, capture_output=True,
                             check=True).stdout.decode())
masked += n
leaks += [f'커밋 메시지: {kind}' for _, kind in find_leaks(msg)]
msg_file.write_text(msg, encoding='utf-8')

if leaks:
    print('중단: 공개 미러에 넣으면 안 되는 값이 남아 있음 (커밋·push 안 함)')
    print('\n'.join(sorted(set(leaks))))
    print('실제 값이면 .env·금지어 목록에 있는지 확인하거나 파일을 EXCLUDE 에 넣을 것')
    sys.exit(1)
print(f'검사 통과: 파일 {len(targets)}개, 가린 곳 {masked}개, 제외 파일 {len(EXCLUDE)}개, 금지어 {deny}개')
if short:
    print(f'짧아서(5자 미만) 안 가린 값: {", ".join(sorted(short))}')
PY

git -C "$MIRROR" add -A
echo "== 미러에서 바뀔 파일"
git -C "$MIRROR" diff --cached --name-status
echo "== 커밋 메시지: $(cat "$MSG_FILE")"
if [ "$MODE" = --check ]; then
  restore; echo "미리보기만 함 — push 안 함. 확인 후 --push"; exit 0
fi
if git -C "$MIRROR" diff --cached --quiet; then echo "mirror: 변경 없음"; exit 0; fi
git -C "$MIRROR" -c user.name="황민영" -c user.email=yakc0202@gmail.com commit -q -F "$MSG_FILE"  # 미러 작성자는 하나로
git -C "$MIRROR" push -q origin main
echo "mirror push 완료: $(git -C "$MIRROR" log -1 --oneline)"
