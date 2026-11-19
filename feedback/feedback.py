#!/usr/bin/env python3
"""피드백 방 수집·배포 도우미 (LLM을 부르지 않음).

collect  : 10분마다(LaunchAgent) 피드백 방을 읽어 inbox.jsonl에 쌓는다.
           새 피드백이 있거나 '처리'라고 쓰면 worker를 백그라운드로 띄운다.
           'push' → report로 올린 커밋이 HEAD와 같으면 push·미러 반영 후 처리한 메시지를 지운다.
worker   : 새 Claude(claude -p)를 띄워 피드백을 고치고 커밋·보고하게 한다. push·미러는 못 하게 막는다.
report   : Claude가 고친 뒤 부른다. 바뀐 점을 방에 보내고 push 대기 커밋을 기록한다.
           python3 feedback/feedback.py report <commit> <보고문 파일> <처리한 inbox 메시지 id,...>
note     : 고칠 것이 없을 때 Claude가 부른다. 보고문만 방에 보낸다.
           python3 feedback/feedback.py note <보고문 파일> [<처리한 inbox 메시지 id,...>]
status   : 쌓인 피드백·대기 커밋을 보여 준다.

설정(.env): LOG_BOT_TOKEN(로그용 봇), FEEDBACK_CHAT(방 ID), FEEDBACK_OWNER_ID(명령을 받을 사용자 ID),
           CLAUDE_CODE_OAUTH_TOKEN(worker 로그인), FEEDBACK_WORKER_TIMEOUT(초, 기본 1200).
상태: ~/.local/state/telegram-bot/feedback/ (저장소 밖).
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("ENV_FILE", Path.home() / ".config/telegram-bot/.env"))
STATE = Path(os.environ.get("FEEDBACK_STATE_DIR", Path.home() / ".local/state/telegram-bot/feedback"))
INBOX = STATE / "inbox.jsonl"      # 사용자가 쓴 피드백(명령 제외)
OFFSET = STATE / "offset"          # getUpdates 확인 위치
WORKER = STATE / "worker.pid"      # 처리 중인 worker(겹쳐 띄우지 않음)
WORKER_LOG = STATE / "worker.log"  # 마지막 worker 출력(방에는 안 보냄)
PENDING = STATE / "pending.json"   # report가 남긴 push 대기 커밋
EXTRA_IDS = STATE / "extra_ids"    # 명령·보고 메시지 id(정리 때 같이 지움)
CLAUDE_BIN = os.environ.get("FEEDBACK_CLAUDE_BIN", "/opt/homebrew/bin/claude")

# worker가 쓸 수 있는 도구. 그 밖은 dontAsk 모드라 묻지 않고 거부된다.
WORKER_ALLOWED = [
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(python3:*)", "Bash(.venv/bin/python3:*)", "Bash(/usr/bin/python3:*)",
    "Bash(PYTHONPATH=*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git show:*)", "Bash(git add:*)", "Bash(git commit:*)", "Bash(grep:*)", "Bash(tail:*)",
    "Bash(head:*)", "Bash(ls:*)", "Bash(wc:*)",
]
WORKER_DENIED = ["Bash(git push:*)", "Bash(./mirror-push.sh:*)", "Bash(git reset:*)",
                 "Bash(git checkout:*)", "Bash(rm:*)", "Bash(sudo:*)", "Bash(launchctl:*)"]
WORKER_PROMPT = """피드백 방에 새 피드백이 왔다. AGENTS.md의 '피드백 방' 규칙대로 처리하라.
1. SYNC.md를 읽고 `/usr/bin/python3 feedback/feedback.py status`로 쌓인 피드백을 본다.
2. 고칠 수 있는 것은 고치고 py_compile과 실제 데이터로 시험한 뒤 REQUESTS.md에 기록하고 커밋한다.
3. 바뀐 점을 사용자가 읽을 한국어 보고문으로 파일에 쓰고
   `/usr/bin/python3 feedback/feedback.py report <커밋> <보고문 파일> <처리한 id,...>`로 보고한다.
   고칠 것이 없으면 `feedback.py note <보고문 파일> [처리한 id,...]`로 이유만 보고한다.
4. 판단이 필요하거나 고칠 수 없는 피드백은 처리 id에서 빼고 보고문에 이유를 쓴다.
git push와 mirror-push.sh는 하지 않는다. 배포는 사용자가 방에 'push'라고 쓰면 수집기가 한다."""

WAKE_WORDS = {"처리", "처리해", "고쳐"}
PUSH_WORDS = {"push", "푸시", "배포"}
STATUS_WORDS = {"상태", "status"}


def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


ENV = load_env()
TOKEN = ENV["LOG_BOT_TOKEN"]
CHAT = int(ENV["FEEDBACK_CHAT"])
OWNER = int(ENV["FEEDBACK_OWNER_ID"])


def api(method, **params):
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["result"]


def send(text):
    msg = api("sendMessage", chat_id=CHAT, text=text[:4000], disable_web_page_preview=True)
    remember_extra(msg["message_id"])
    return msg


def remember_extra(mid):
    with EXTRA_IDS.open("a") as f:
        f.write(f"{mid}\n")


def inbox_items():
    if not INBOX.exists():
        return []
    return [json.loads(l) for l in INBOX.read_text().splitlines() if l.strip()]


def worker_alive():
    try:
        os.kill(int(WORKER.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def to_item(m):
    """메시지에서 고칠 때 필요한 것만 남긴다(전달 원본 채널·글 번호 포함)."""
    item = {"id": m["message_id"], "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(m["date"])),
            "text": m.get("text") or m.get("caption") or ""}
    origin = m.get("forward_origin")
    if origin:
        chat = origin.get("chat") or {}
        item["forward"] = {"chat_id": chat.get("id"), "title": chat.get("title"),
                           "message_id": origin.get("message_id"),
                           "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(origin.get("date", 0)))}
    if m.get("reply_to_message"):
        item["reply_to"] = m["reply_to_message"]["message_id"]
    for e in m.get("entities", []) + m.get("caption_entities", []):
        if e.get("type") == "text_link":
            item.setdefault("links", []).append(e["url"])
    if m.get("photo") or m.get("document"):
        item["has_media"] = True
    return item


def collect():
    offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
    updates = api("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])
    new = 0
    for u in updates:
        offset = u["update_id"] + 1
        m = u.get("message")
        if not m or m["chat"]["id"] != CHAT or m.get("from", {}).get("id") != OWNER:
            continue
        word = (m.get("text") or "").strip().lower()
        if word in WAKE_WORDS:
            remember_extra(m["message_id"])
            on_wake()
        elif word in PUSH_WORDS:
            remember_extra(m["message_id"])
            on_push()
        elif word in STATUS_WORDS:
            remember_extra(m["message_id"])
            on_status()
        else:
            with INBOX.open("a") as f:
                f.write(json.dumps(to_item(m), ensure_ascii=False) + "\n")
            new += 1
    OFFSET.write_text(str(offset))
    if new:
        print(f"{time.strftime('%F %T')} 피드백 {new}건 쌓음")
        # 새 피드백이 있으면 '처리'를 기다리지 않고 바로 worker를 띄운다(처리 중이면 끝난 뒤 다음 수집 때)
        if not worker_alive():
            on_wake()


def on_wake():
    n = len(inbox_items())
    if not n:
        send("쌓인 피드백이 없습니다.")
        return
    if worker_alive():
        send(f"이미 처리 중입니다. 끝나면 바뀐 점을 여기에 보냅니다(쌓인 피드백 {n}건).")
        return
    subprocess.Popen(["/usr/bin/python3", str(Path(__file__).resolve()), "worker"], cwd=REPO,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    send(f"피드백 {n}건 처리를 시작합니다. 고치면 바뀐 점을 여기에 보냅니다.")


def worker():
    """새 Claude를 띄워 피드백을 처리한다. 겹치지 않게 pid를 남기고, 실패·시간 초과는 방에 알린다."""
    if worker_alive():
        return
    WORKER.write_text(str(os.getpid()))
    before = (PENDING.read_text() if PENDING.exists() else "", EXTRA_IDS.read_text() if EXTRA_IDS.exists() else "")
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
           "LANG": "en_US.UTF-8", "CLAUDE_CODE_OAUTH_TOKEN": ENV.get("CLAUDE_CODE_OAUTH_TOKEN", "")}
    cmd = [CLAUDE_BIN, "-p", WORKER_PROMPT, "--permission-mode", "dontAsk",
           "--allowedTools", *WORKER_ALLOWED, "--disallowedTools", *WORKER_DENIED]
    timeout = int(ENV.get("FEEDBACK_WORKER_TIMEOUT", "1200"))
    try:
        with WORKER_LOG.open("w") as log:
            r = subprocess.run(cmd, cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, timeout=timeout)
        failed = f"종료 코드 {r.returncode}" if r.returncode else ""
    except subprocess.TimeoutExpired:
        failed = f"{timeout // 60}분 안에 끝나지 않아 멈춤"
    finally:
        WORKER.unlink(missing_ok=True)
    after = (PENDING.read_text() if PENDING.exists() else "", EXTRA_IDS.read_text() if EXTRA_IDS.exists() else "")
    if failed:
        send(f"피드백 처리 실패({failed}). 맥의 {WORKER_LOG}를 확인해 주세요.")
    elif after == before:
        send(f"피드백 처리가 보고 없이 끝났습니다. 맥의 {WORKER_LOG}를 확인해 주세요.")


def on_status():
    items = inbox_items()
    lines = [f"쌓인 피드백 {len(items)}건, {'처리 중' if worker_alive() else '대기'}"]
    if PENDING.exists():
        p = json.loads(PENDING.read_text())
        lines.append(f"push 대기 커밋 {p['commit'][:7]} ('push'라고 쓰면 배포)")
    send("\n".join(lines))


def run(*cmd):
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=600)
    return r.returncode, (r.stdout + r.stderr).strip()


def on_push():
    if not PENDING.exists():
        send("push 대기 중인 수정이 없습니다.")
        return
    p = json.loads(PENDING.read_text())
    _, head = run("git", "rev-parse", "HEAD")
    _, dirty = run("git", "status", "--porcelain", "--untracked-files=no")
    if head != p["commit"] or dirty:
        send(f"push 안 함: 보고한 커밋({p['commit'][:7]})과 지금 저장소(HEAD {head[:7]}"
             f"{', 커밋 안 된 변경 있음' if dirty else ''})가 다릅니다. Claude 세션에서 확인해 주세요.")
        return
    code, out = run("git", "push", "origin", "main")
    if code:
        send(f"push 실패:\n{out[-1500:]}")
        return
    code, out = run("./mirror-push.sh", "--push")
    mirror = out.splitlines()[-1] if out else ""
    PENDING.unlink()
    handled = set(p["handled"])
    cleanup(handled)
    send(f"push 완료 ({p['commit'][:7]}). 봇이 재시작됩니다."
         + (f"\n미러: {mirror}" if code == 0 else f"\n미러 반영 실패(세션에서 확인): {mirror}")
         + f"\n남은 피드백 {len(inbox_items())}건")


def cleanup(handled, extras=True):
    """처리한 피드백과(extras면) 명령·보고 메시지를 지운다. 봇은 48시간 지난 메시지는 못 지운다."""
    items = inbox_items()
    keep = [i for i in items if i["id"] not in handled]
    ids = sorted(handled | {int(x) for x in EXTRA_IDS.read_text().split()}
                 if extras and EXTRA_IDS.exists() else handled)
    failed = 0
    for i in range(0, len(ids), 100):
        try:
            api("deleteMessages", chat_id=CHAT, message_ids=ids[i:i + 100])
        except Exception:
            # 묶음 삭제가 실패하면(48시간 지난 메시지 섞임 등) 하나씩 지운다
            for mid in ids[i:i + 100]:
                try:
                    api("deleteMessage", chat_id=CHAT, message_id=mid)
                except Exception:
                    failed += 1
    INBOX.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in keep))
    if extras:
        EXTRA_IDS.write_text("")
    if failed:
        send(f"메시지 {failed}개는 48시간이 지나 봇이 지우지 못했습니다. 직접 지워 주세요.")


def report(commit, text_file, handled_csv):
    handled = [int(x) for x in handled_csv.split(",") if x.strip()]
    if PENDING.exists():  # push 전에 다시 고쳤으면 앞서 처리한 피드백도 같이 지운다
        handled = sorted(set(handled) | set(json.loads(PENDING.read_text())["handled"]))
    PENDING.write_text(json.dumps({"commit": commit, "handled": handled}))
    send(Path(text_file).read_text().strip() + f"\n\n커밋 {commit[:7]}. 배포하려면 'push'라고 쓰세요.")


def note(text_file, handled_csv=""):
    """고칠 것이 없을 때: 보고문만 보내고, 처리한 피드백은 inbox에서 빼고 메시지를 지운다."""
    send(Path(text_file).read_text().strip())
    handled = {int(x) for x in handled_csv.split(",") if x.strip()}
    if handled:
        cleanup(handled, extras=False)


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if cmd == "collect":
        collect()
    elif cmd == "report":
        report(*sys.argv[2:5])
    elif cmd == "note":
        note(*sys.argv[2:4])
    elif cmd == "worker":
        worker()
    elif cmd == "status":
        print(json.dumps({"inbox": inbox_items(), "pending": json.loads(PENDING.read_text()) if PENDING.exists() else None},
                         ensure_ascii=False, indent=1))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
