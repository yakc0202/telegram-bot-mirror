#!/bin/sh
# 공개 미러(telegram-bot-mirror)로 이 저장소의 추적 파일을 내보낸다. 보안 값은 내보내지 않는다.
#   ./mirror-push.sh           미리보기: 바뀔 파일·검사 결과만 보여주고 되돌림(push 안 함)
#   ./mirror-push.sh --push    실제 커밋·push(일반 push, force-push 아님)
# 가릴 값은 ~/.config/telegram-bot/.env 에서 직접 읽는다(토큰·키·해시·전화번호·채팅방 ID).
# 값 목록 파일을 따로 두지 않으므로 실제 값은 어떤 저장소에도 커밋되지 않는다.
# 가린 뒤에도 보안 값 모양이 남아 있으면 커밋·push 없이 멈춘다.
set -eu
MODE=${1:---check}
case "$MODE" in --check|--push) ;; *) echo "알 수 없는 옵션: $MODE"; exit 1 ;; esac
SRC=$(cd "$(dirname "$0")" && git rev-parse --show-toplevel)
MIRROR=${MIRROR_DIR:-"$HOME/.cache/telegram-bot-mirror"}
ENV_FILE=${ENV_FILE:-"$HOME/.config/telegram-bot/.env"}
[ -f "$ENV_FILE" ] || { echo "중단: $ENV_FILE 가 없음 (가릴 값을 읽을 수 없음)"; exit 1; }
[ -d "$MIRROR/.git" ] || git clone -q https://github.com/yakc0202/telegram-bot-mirror "$MIRROR"
git -C "$MIRROR" fetch -q origin
git -C "$MIRROR" checkout -q -f main
git -C "$MIRROR" reset -q --hard origin/main
git -C "$MIRROR" clean -fdq
restore() { git -C "$MIRROR" checkout -q -f main; git -C "$MIRROR" reset -q --hard origin/main; git -C "$MIRROR" clean -fdq; }

python3 - "$SRC" "$MIRROR" "$ENV_FILE" <<'PY' || { restore; exit 1; }
import re, shutil, subprocess, sys
from pathlib import Path
src, mirror, env_file = map(Path, sys.argv[1:])

# 공개하지 않는 파일
EXCLUDE = {'SYNC.md'}  # 지금 상태 요약(운영 맥락)
# 경로를 바꿔 내보내는 파일: 미러에서 워크플로가 실행되지 않게 참고용으로만 둔다
RENAME = {'.github/workflows/deploy.yml': 'docs/deploy.yml.reference'}
# 미러에만 있는 파일(지우지 않음)
MIRROR_ONLY = {'.env.example'}

# .env 에서 보안 값 수집: 이름에 TOKEN/KEY/HASH/API_ID/PHONE/CHAT 이 들어간 항목
SECRET_NAME = re.compile(r'(TOKEN|KEY|HASH|API_ID|PHONE|CHAT)')
pairs = []
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
            pairs.append((v, f'<{name}>'))
pairs.sort(key=lambda p: -len(p[0]))  # 긴 값부터 가림
pairs.append((str(Path.home()), '~'))  # 맥 계정 이름이 든 홈 경로


def public_requests(text):
    # REQUESTS.md: '<!-- mirror:skip -->'가 붙은 줄을 빼고, 제목 줄(#)에 붙어 있으면 다음 제목 전까지
    # 그 항목 전체를 뺀다
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith('#'):
            skip = 'mirror:skip' in line
        if skip or 'mirror:skip' in line:
            continue
        out.append(line)
    return ''.join(out)

generic = [
    r'\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b',   # 텔레그램 봇 토큰
    r'\bAIza[0-9A-Za-z_-]{35}\b',           # Google(Gemini) API 키
    r'\bsk-(ant-|proj-)?[A-Za-z0-9_-]{20,}', # Anthropic/OpenAI 키
    r'\bnvapi-[A-Za-z0-9_-]{20,}',          # NVIDIA 키
    r'\b[0-9a-f]{32}\b',                    # 텔레그램 API hash 모양
    r'\+82[0-9 -]{8,}',                     # 전화번호
    r'-100[0-9]{9,}',                       # 채널·그룹 ID
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
]
ALLOW = {'-1001234567890'}  # 코드 주석의 예시 ID

tracked = [f for f in subprocess.run(['git', 'ls-files', '-z'], cwd=src, capture_output=True,
           check=True).stdout.decode().split('\0') if f and f not in EXCLUDE]
targets = {f: RENAME.get(f, f) for f in tracked}
keep = set(targets.values()) | MIRROR_ONLY
for p in list(mirror.rglob('*')):
    rel = p.relative_to(mirror)
    if rel.parts[0] != '.git' and p.is_file() and str(rel) not in keep:
        p.unlink()

leaks, masked = [], 0
for name, out in targets.items():
    dst = mirror / out
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / name, dst)
    if name == 'REQUESTS.md':
        dst.write_text(public_requests(dst.read_text(encoding='utf-8')), encoding='utf-8')
    data = dst.read_bytes()
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        text = None
    if text is not None:
        for real, mask in pairs:
            if real in text:
                masked += text.count(real)
                text = text.replace(real, mask)
        if text.encode('utf-8') != data:
            dst.write_text(text)
        data = text.encode('utf-8')
        for pat in generic:
            leaks += [f'{out}: {m.group(0)[:8]}…' for m in re.finditer(pat, text) if m.group(0) not in ALLOW]
    for real, _ in pairs:
        if real.encode('utf-8') in data:
            leaks.append(f'{out}: {real[:4]}… ({"바이너리" if text is None else "텍스트"})')
if leaks:
    print('중단: 공개 미러에 넣으면 안 되는 값이 남아 있음 (커밋·push 안 함)')
    print('\n'.join(sorted(set(leaks))))
    print('실제 값이면 .env 에 있는지 확인하거나 파일을 EXCLUDE 에 넣을 것')
    sys.exit(1)
print(f'검사 통과: 파일 {len(targets)}개, 가린 곳 {masked}개, 제외 파일 {len(EXCLUDE)}개')
PY

git -C "$MIRROR" add -A
echo "== 미러에서 바뀔 파일"
git -C "$MIRROR" diff --cached --name-status
if [ "$MODE" = --check ]; then
  restore; echo "미리보기만 함 — push 안 함. 확인 후 --push"; exit 0
fi
if git -C "$MIRROR" diff --cached --quiet; then echo "mirror: 변경 없음"; exit 0; fi
git -C "$MIRROR" commit -q -m "$(git -C "$SRC" log -1 --format=%s)"
git -C "$MIRROR" push -q origin main
echo "mirror push 완료: $(git -C "$MIRROR" log -1 --oneline)"
