"""
텔레그램 여러 채팅방의 주식 정보 메시지를 모아서
- 완전히 동일한 메시지는 1개만
- 표현은 다르지만 내용이 같은 메시지도 대표로 1개만
남겨서 지정한 채널로 전달하는 스크립트.

동작 방식:
1) 지정한 SOURCE_CHATS 의 새 메시지를 실시간으로 수신
2) 텍스트를 정규화해 해시 비교 → 완전 동일 메시지는 즉시 스킵
3) 메시지에 포함된 URL을 추출해 최근 기록과 비교 → 같은 링크(기사)를 공유하면
   문구가 완전히 달라도 즉시 중복으로 판단 (예: "또 시작" vs "[속보] ...", 같은 기사 링크)
4) 최근 N분 내 대표 메시지들과 키워드/가격 유사도 비교
   - 유사도가 매우 높으면 → 중복으로 판단, 스킵
   - 유사도가 애매하면 → Claude API에게 "같은 정보인지" 물어봐서 최종 판단
   - 유사도가 낮으면 → 새로운 정보로 판단
5) 새로운 정보로 판단된 메시지만 요약(또는 원문 미리보기)으로 만들어 DIGEST_CHAT 으로 전송
"""

import os
import time
import re
import json
import html
import base64
import sqlite3
import hashlib
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

# .env를 실행 위치와 무관하게 고정된 경로에서 읽음 (cron/systemd 등 다른 작업 디렉토리에서
# 실행되거나, Docker가 아닌 로컬 실행 시에도 항상 같은 설정 파일을 쓰도록 하기 위함).
# 이미 설정된 환경변수(예: Docker의 env_file로 주입된 값)는 덮어쓰지 않음.
from dotenv import load_dotenv

_FIXED_ENV_PATH = Path.home() / ".config" / "telegram-bot" / ".env"
if _FIXED_ENV_PATH.exists():
    load_dotenv(_FIXED_ENV_PATH)
else:
    load_dotenv()  # 고정 경로에 없으면 현재 작업 디렉토리의 .env를 시도 (있으면)


from dotenv import load_dotenv
from pathlib import Path
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaWebPage, WebPage, MessageMediaPoll, MessageMediaPhoto
from anthropic import Anthropic
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_FIXED_ENV_PATH = Path.home() / ".config" / "telegram-bot" / ".env"
if _FIXED_ENV_PATH.exists():
    load_dotenv(_FIXED_ENV_PATH)
else:
    load_dotenv()  # 고정 경로에 없으면 기존처럼 현재 작업 폴더에서 .env를 찾음 (Docker 등 호환용)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("stock-dedup")

# ── 로그를 텔레그램 채널로도 전송하기 위한 핸들러 ──
# 콘솔에 찍히는 모든 로그(기본 root logger에 붙기 때문에 telethon 등 다른 라이브러리
# 로그도 포함)를 큐에 쌓아두고, 백그라운드 태스크가 몇 초 간격으로 모아서 전송함.
# INFO 로그는 A방, WARNING 이상은 B방으로 서로 다른 채널에 나눠서 보냄.
_log_queue_info: "asyncio.Queue[str]" = None    # main() 시작 시 이벤트루프 안에서 생성
_log_queue_warning: "asyncio.Queue[str]" = None


class _ExactLevelFilter(logging.Filter):
    """딱 지정한 레벨과 정확히 일치하는 로그만 통과시킴 (INFO 전용 채널이 WARNING/ERROR까지
    받아버리지 않도록, INFO 핸들러에만 적용)."""

    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == self.level


class _TelegramQueueLogHandler(logging.Handler):
    def __init__(self, queue_getter):
        super().__init__()
        self._queue_getter = queue_getter  # 지연 평가용 (핸들러 생성 시점엔 큐가 아직 없음)

    def emit(self, record):
        queue = self._queue_getter()
        if queue is None:
            return
        try:
            msg = self.format(record)
            queue.put_nowait(msg)
        except Exception:
            pass  # 로그 전송 실패가 로그를 또 남기는 무한루프를 막기 위해 조용히 무시


async def _log_shipper_loop(queue_getter, bot_token: str, chat_id):
    """큐에 쌓인 로그를 몇 초 간격으로 모아서 지정된 채널로 전송하는 백그라운드 루프."""
    import sys as _sys  # log.* 호출로 인한 재귀를 피하려고 stderr에 직접 출력

    while True:
        await asyncio.sleep(10)
        queue = queue_getter()
        if queue is None:
            continue
        lines = []
        while not queue.empty():
            try:
                lines.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not lines:
            continue

        combined = "\n".join(lines)
        # 텔레그램 메시지 길이 제한(4096자)에 맞춰 청크로 나눠 전송
        chunk_size = 3900
        chunks = [combined[i:i + chunk_size] for i in range(0, len(combined), chunk_size)]
        for chunk in chunks:
            try:
                resp = await asyncio.to_thread(
                    requests.post,
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"<pre>{html.escape(chunk)}</pre>",
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "disable_notification": True,  # 로그는 알림 없이 조용히 쌓이도록
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    print(f"[log-shipper] 로그 전송 실패: {resp.status_code} {resp.text}", file=_sys.stderr)
            except Exception as e:
                print(f"[log-shipper] 로그 전송 중 예외 발생: {e}", file=_sys.stderr)


DB_PATH = os.environ.get("DB_PATH", "dedup_history.db")

# "8시/18시 정리를 마지막으로 언제 보냈는지" 기록은 SQLite DB 안에 두면, DB가 손상돼서
# 자동 백업+새 DB로 초기화될 때 이 기록도 같이 사라져서 catch-up이 "안 보냈다"고 착각해
# 이미 보낸 정리를 중복 재발송하는 문제가 있었음(실제로 겪은 장애). 이 기록만큼은 DB와
# 완전히 분리된, 훨씬 단순하고 손상에 강한 JSON 파일에 별도로 저장함.
DIGEST_SENT_STATE_PATH = os.environ.get("DIGEST_SENT_STATE_PATH") or str(Path(DB_PATH).parent / "digest_sent_state.json")


def get_digest_sent_time(trigger_hour: int) -> Optional[str]:
    try:
        with open(DIGEST_SENT_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(str(trigger_hour))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def set_digest_sent_time(trigger_hour: int, iso_timestamp: str):
    data = {}
    try:
        with open(DIGEST_SENT_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    data[str(trigger_hour)] = iso_timestamp
    # 임시 파일에 먼저 쓰고 원자적으로 교체(rename)해서, 쓰기 도중 강제종료돼도 이 파일
    # 자체가 반쯤 쓰인 상태로 손상되는 걸 방지함.
    tmp_path = DIGEST_SENT_STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_path, DIGEST_SENT_STATE_PATH)


# 사진/미디어를 메모리 대신 디스크로 내려받을 때 쓰는 임시 폴더. 매번 os.remove로 지우지만,
# 혹시 비정상 종료(SIGKILL 등)로 정리가 안 된 파일이 남아있을 수 있어 시작 시 한 번 청소함.
TEMP_MEDIA_DIR = os.environ.get("TEMP_MEDIA_DIR") or str(Path(tempfile.gettempdir()) / "telegram_stock_dedup_media")
os.makedirs(TEMP_MEDIA_DIR, exist_ok=True)
for _stale_file in Path(TEMP_MEDIA_DIR).glob("tg_*"):
    try:
        _stale_file.unlink()
    except OSError:
        pass
SESSION_PATH = os.environ.get("SESSION_PATH", "stock_dedup_session")

# ── 설정 로드 ──
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE = os.environ.get("TELEGRAM_PHONE")

# 알림용 봇 토큰 (선택). 설정하면 전달 후 봇 명의로 알림 메시지를 추가로 보내서
# 텔레그램 알림이 정상적으로 오도록 함 (본인 계정이 보낸 메시지는 알림이 안 오기 때문)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# 로그 전용 봇/채널 (선택). 둘 다 설정하면, 콘솔에 찍히는 모든 로그(우리 코드 + Telethon 내부 로그
# 포함)를 이 채널로도 몇 초 간격으로 묶어서 전송함. 알림용 봇과는 별도 봇을 쓰는 걸 권장
# (로그가 많아서 알림 채널이 시끄러워지는 걸 방지).
# INFO 로그는 LOG_CHAT_INFO(A방), WARNING 이상은 LOG_CHAT_WARNING(B방)으로 나눠서 전송함.
LOG_BOT_TOKEN = os.environ.get("LOG_BOT_TOKEN")
_log_chat_info_raw = os.environ.get("LOG_CHAT_INFO", "").strip()
# LOG_CHAT_WARNING이 없으면 예전 이름인 LOG_CHAT을 그대로 씀 (하위 호환)
_log_chat_warning_raw = os.environ.get("LOG_CHAT_WARNING", "").strip() or os.environ.get("LOG_CHAT", "").strip()

# 하루 동안(예: 06시~18시) 새로 등록된 정보들을 모아서, 지정한 시각에 종합 정리 + 인사이트를
# 한 번에 만들어 올리는 기능 (기본 꺼짐, opt-in). 시각은 모두 한국시간(KST) 기준.
DAILY_DIGEST_ENABLED = os.environ.get("DAILY_DIGEST_ENABLED", "false").strip().lower() in ("1", "true", "yes")
DAILY_DIGEST_START_HOUR = int(os.environ.get("DAILY_DIGEST_START_HOUR", 6))
DAILY_DIGEST_HOUR = int(os.environ.get("DAILY_DIGEST_HOUR", 18))
# 야간 정리: DAILY_DIGEST_HOUR(18시)부터 다음날 이 시각까지 모아서, 이 시각에 전송.
# DAILY_DIGEST_ENABLED가 켜져 있으면 이것도 같이 켜짐 (같은 on/off 스위치 공유).
NIGHT_DIGEST_END_HOUR = int(os.environ.get("NIGHT_DIGEST_END_HOUR", 8))
_digest_chat_raw = os.environ.get("SUMMARY_CHAT", "").strip()  # 정리(주간/야간)를 보낼 채널 (선택, 없으면 DIGEST_CHAT)

# Telethon 라이브러리의 알려진 이슈("Server sent a very old message" 반복 후 새 메시지 수신이
# 조용히 멈추는 현상)를 예방하기 위해, 일정 시간마다 예방 차원으로 재연결함.
RECONNECT_INTERVAL_HOURS = float(os.environ.get("RECONNECT_INTERVAL_HOURS", 12))

# 재전달 감지용 processed_message_ids가 무한정 쌓이지 않도록, 이 시간(시간 단위)이 지난
# 항목은 주기적으로 정리함. 재연결 직후 재전달은 보통 몇 분 내에 일어나므로 넉넉한 값.
PROCESSED_ID_RETENTION_HOURS = float(os.environ.get("PROCESSED_ID_RETENTION_HOURS", 2))

def parse_chat_ref(raw: str):
    """'-1001234567890' 같은 숫자 ID 문자열은 int로 변환, username/링크는 그대로 문자열 유지."""
    raw = raw.strip()
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


SOURCE_CHATS = [parse_chat_ref(c) for c in os.environ["SOURCE_CHATS"].split(",") if c.strip()]

# 요약/알림을 보낼 채널 (필수). 원본 전달 없이 이 채널로 요약(또는 원문 미리보기)만 보냄.
DIGEST_CHAT = parse_chat_ref(os.environ["DIGEST_CHAT"])

# 정리(06~18시, 18~08시)를 보낼 채널. 안 정해두면 DIGEST_CHAT과 같이 씀.
SUMMARY_CHAT = parse_chat_ref(_digest_chat_raw) if _digest_chat_raw else DIGEST_CHAT

LOG_CHAT_INFO = parse_chat_ref(_log_chat_info_raw) if _log_chat_info_raw else None
LOG_CHAT_WARNING = parse_chat_ref(_log_chat_warning_raw) if _log_chat_warning_raw else None

if LOG_BOT_TOKEN and LOG_CHAT_INFO:
    _handler_info = _TelegramQueueLogHandler(lambda: _log_queue_info)
    _handler_info.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _handler_info.addFilter(_ExactLevelFilter(logging.INFO))  # 딱 INFO만 (WARNING 이상은 안 감)
    logging.getLogger().addHandler(_handler_info)

if LOG_BOT_TOKEN and LOG_CHAT_WARNING:
    _handler_warning = _TelegramQueueLogHandler(lambda: _log_queue_warning)
    _handler_warning.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _handler_warning.setLevel(logging.WARNING)  # WARNING 이상만
    logging.getLogger().addHandler(_handler_warning)

# 잡담/주식과 무관한 메시지를 걸러낼지 여부 (기본 켜짐)
CHITCHAT_FILTER_ENABLED = os.environ.get("CHITCHAT_FILTER_ENABLED", "true").strip().lower() in ("1", "true", "yes")

# ── LLM 제공자 선택 (anthropic / openai / gemini) ──
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash")

# 여러 개의 Gemini API 키를 콤마로 등록하면, 요청마다 순서를 돌려가며 사용하고
# 특정 키가 실패(rate limit 등)하면 같은 호출 안에서 자동으로 다음 키를 시도함.
# 각 키가 별도 프로젝트로 발급된 것이면 사실상 무료 티어 한도가 키 개수만큼 늘어나는 효과가 있음.
_gemini_keys_raw = os.environ.get("GEMINI_API_KEYS", "").strip()
GEMINI_API_KEYS = [k.strip() for k in _gemini_keys_raw.split(",") if k.strip()]
if not GEMINI_API_KEYS and GEMINI_API_KEY:
    GEMINI_API_KEYS = [GEMINI_API_KEY]  # GEMINI_API_KEYS 안 쓰면 기존 단일 키를 그대로 사용

if LLM_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
    raise RuntimeError("LLM_PROVIDER=anthropic 인데 ANTHROPIC_API_KEY가 .env에 없습니다.")
if LLM_PROVIDER == "openai" and not OPENAI_API_KEY:
    raise RuntimeError("LLM_PROVIDER=openai 인데 OPENAI_API_KEY가 .env에 없습니다.")
if LLM_PROVIDER == "gemini" and not GEMINI_API_KEYS:
    raise RuntimeError("LLM_PROVIDER=gemini 인데 GEMINI_API_KEY(S)가 .env에 없습니다.")

# 1순위 LLM_PROVIDER 호출이 실패(크레딧 소진 등)하면 순서대로 시도할 대체 provider 목록.
# 해당 provider의 API 키가 없으면 자동으로 건너뜀. 예: LLM_FALLBACK_PROVIDERS=openai,gemini
LLM_FALLBACK_PROVIDERS = [
    p.strip().lower() for p in os.environ.get("LLM_FALLBACK_PROVIDERS", "").split(",") if p.strip()
]

DEDUP_WINDOW_MINUTES = int(os.environ.get("DEDUP_WINDOW_MINUTES", 1440))  # 기본 24시간
THRESHOLD_HIGH = float(os.environ.get("KEYWORD_THRESHOLD_HIGH", 0.6))
THRESHOLD_LOW = float(os.environ.get("KEYWORD_THRESHOLD_LOW", 0.25))
# 캡션이 짧으면(키워드 수가 적으면) 우연히 겹치는 공통 단어 몇 개만으로 점수가 확 뛸 수 있어서
# ("26년 6월 이마트 매출" vs "26년 6월 신세계 매출" 같은 경우), 이 개수 미만이면 점수가 높아도
# 자동으로 중복 확정하지 않고 반드시 LLM에게 재확인함
MIN_KEYWORDS_FOR_AUTO_DUP = int(os.environ.get("MIN_KEYWORDS_FOR_AUTO_DUP", 6))
# 애매한 유사도 구간에서 LLM에게 재확인시킬 후보를 점수 높은 순으로 최대 몇 개까지 볼지.
# 1개만 보면 우연히 무관한 후보가 1등일 때 진짜 후보를 놓칠 수 있어 넉넉히 잡음.
MAX_LLM_DUP_CANDIDATES = int(os.environ.get("MAX_LLM_DUP_CANDIDATES", 3))
# 유사도가 자동확정 기준(THRESHOLD_HIGH)을 넘어도, 두 메시지의 키워드 개수 차이가 이 배율보다
# 크면(예: 한쪽이 다른 쪽 분량의 1.8배 넘게 크면) 자동확정하지 않고 LLM에게 재확인시킴.
# 짧은 텍스트가 긴 텍스트 안에 통째로 '포함'된 형태(예: 경제지표 일정표가 마켓 브리핑 안에
# 그대로 들어있는 경우)일 수 있는데, 이러면 유사도는 높게 나와도 긴 쪽에만 있는 추가 정보
# (시황 요약, 지수 등락률 등)를 자동확정이 놓쳐버리는 문제가 있어서 추가함.
MAX_AUTO_DUP_SIZE_RATIO = float(os.environ.get("MAX_AUTO_DUP_SIZE_RATIO", 1.4))
# 같은 URL(기사)을 공유해도, 첨부된 문구의 키워드 유사도가 이 값보다 낮으면
# "같은 기사에 다른 의견/코멘트를 단 경우"일 수 있으므로 바로 중복 처리하지 않고 Claude에게 재확인함
URL_MATCH_SCORE_THRESHOLD = float(os.environ.get("URL_MATCH_SCORE_THRESHOLD", 0.15))

# 전달된 메시지에 LLM이 만든 한 줄 요약을 붙일지 여부 (기본 꺼짐, 토큰 비용 발생하므로 opt-in)
SUMMARIZE_ENABLED = os.environ.get("SUMMARIZE_ENABLED", "false").strip().lower() in ("1", "true", "yes")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# 최근 "대표로 인정된" 메시지들을 보관하는 버퍼 (SQLite에서 로드되어 채워짐)
# 각 항목: {"text", "keywords": set, "numbers": set, "hash", "time"}
recent_buffer: list[dict] = []
seen_hashes: set[str] = set()

_db_conn: Optional[sqlite3.Connection] = None


def get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        # 기본 text_factory는 TEXT 컬럼을 읽을 때 엄격하게 UTF-8 디코딩을 시도하다가,
        # 깨진 바이트가 하나라도 있으면(예: 예전 강제종료로 WAL 기록이 중간에 끊긴 경우)
        # sqlite3.OperationalError를 던지며 그 즉시 프로세스 전체가 죽어버린다. 이러면 시작할
        # 때마다 같은 손상된 행을 다시 만나서 무한 재시작 루프에 빠지게 됨(실제로 겪은 장애).
        # errors="replace"로 디코딩 실패한 바이트는 크래시 대신 대체 문자(�)로 바꿔서
        # 최소한 프로세스는 계속 살아있도록 함.
        _db_conn.text_factory = lambda b: b.decode("utf-8", errors="replace")

        # 무결성 검사: 반복된 강제종료(SIGKILL) 등으로 DB 파일 자체가 디스크 레벨에서
        # 손상("database disk image is malformed")되면, 이후 모든 쓰기/읽기가 그 즉시
        # 예외를 던지며 무한 재시작 루프에 빠짐. 시작 시점에 미리 감지해서, 손상된 파일은
        # 타임스탬프를 붙여 백업해두고 새 DB로 새출발함 (중복판단 이력은 초기화되지만,
        # 봇 자체가 영구적으로 멈추는 것보다는 나음).
        try:
            integrity_result = _db_conn.execute("PRAGMA integrity_check;").fetchone()
            is_healthy = bool(integrity_result) and integrity_result[0] == "ok"
        except sqlite3.DatabaseError:
            is_healthy = False

        if not is_healthy:
            log.error(f"DB 파일이 손상된 것으로 감지됨({DB_PATH}). 손상된 파일을 백업하고 새 DB로 시작합니다.")
            try:
                _db_conn.close()
            except Exception:
                pass
            backup_suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            for ext in ("", "-wal", "-shm"):
                src = DB_PATH + ext
                if os.path.exists(src):
                    try:
                        os.rename(src, f"{src}.corrupted-{backup_suffix}")
                    except OSError as e:
                        log.error(f"손상된 DB 파일({src}) 백업 실패: {e}")
            log.error("손상된 DB 백업 완료. 새 DB로 다시 연결합니다 (중복판단 이력 초기화됨).")
            _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _db_conn.text_factory = lambda b: b.decode("utf-8", errors="replace")

        # WAL(Write-Ahead Logging) 모드: 일반 저널 모드보다 비정상 종료/동시 접근(예: main.py와
        # backfill.py가 같은 DB 파일을 함께 여는 경우)에 훨씬 안전함. 손상(malformed) 위험을 줄임.
        _db_conn.execute("PRAGMA journal_mode=WAL;")
        _db_conn.execute("PRAGMA synchronous=NORMAL;")
        _db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                hash TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                keywords TEXT NOT NULL,
                numbers TEXT NOT NULL,
                urls TEXT NOT NULL DEFAULT '[]',
                source_label TEXT NOT NULL DEFAULT '',
                source_link TEXT NOT NULL DEFAULT '',
                summary_link TEXT NOT NULL DEFAULT '',
                summary_text TEXT NOT NULL DEFAULT '',
                time TEXT NOT NULL
            )
            """
        )
        # 기존(업데이트 전) DB 파일에는 없던 컬럼들을 안전하게 추가 시도
        for ddl in (
            "ALTER TABLE messages ADD COLUMN urls TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE messages ADD COLUMN source_label TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE messages ADD COLUMN source_link TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE messages ADD COLUMN summary_link TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE messages ADD COLUMN summary_text TEXT NOT NULL DEFAULT ''",
        ):
            try:
                _db_conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # 이미 컬럼이 있으면 무시

        # 중복으로 판정된 메시지도 가볍게 기록해둠 (실시간 알림/중복비교에는 안 쓰지만,
        # 혹시 오탐이었을 경우를 대비해 일일 정리를 만들 때 안전망으로 같이 참고함)
        _db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS duplicate_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                source_label TEXT NOT NULL DEFAULT '',
                source_link TEXT NOT NULL DEFAULT '',
                matched_preview TEXT NOT NULL DEFAULT '',
                time TEXT NOT NULL
            )
            """
        )
        # 간단한 key-value 상태 저장용 (예: 어제 고정해둔 일일 정리 메시지 id 기억해뒀다가 해제하는 데 씀)
        _db_conn.execute(
            "CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT)"
        )
        # 전송 실패한 메시지를 임시로 쌓아뒀다가 재시도하기 위한 큐 (메시지량이 많을 때
        # 일시적인 네트워크 오류/재연결 타이밍 등으로 전송이 실패해도 유실되지 않도록 함)
        _db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS send_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                text TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'text',
                source_chat_id TEXT,
                source_message_id INTEGER
            )
            """
        )
        for ddl in (
            "ALTER TABLE send_queue ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'",
            "ALTER TABLE send_queue ADD COLUMN source_chat_id TEXT",
            "ALTER TABLE send_queue ADD COLUMN source_message_id INTEGER",
        ):
            try:
                _db_conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        _db_conn.commit()
    return _db_conn


def load_recent_from_db():
    """프로그램 시작 시, 윈도우(기본 24시간) 내 기록을 DB에서 불러와 메모리 버퍼를 채움."""
    db = get_db()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=DEDUP_WINDOW_MINUTES)
    rows = db.execute(
        "SELECT hash, text, keywords, numbers, urls, source_label, source_link, summary_link, "
        "summary_text, time FROM messages WHERE time >= ?",
        (cutoff.isoformat(),),
    ).fetchall()
    loaded = 0
    for h, text, kw_json, num_json, urls_json, source_label, source_link, summary_link, summary_text, time_str in rows:
        # 행 하나가 손상돼 있어도(JSON 파싱 실패 등) 그 행만 건너뛰고 나머지는 정상적으로
        # 불러오도록 함 — 예전엔 이런 예외 하나가 전체 시작을 막아서 무한 재시작 루프에
        # 빠지는 원인이 됐음.
        try:
            recent_buffer.append({
                "text": text,
                "keywords": set(json.loads(kw_json)),
                "numbers": set(json.loads(num_json)),
                "urls": set(json.loads(urls_json)) if urls_json else set(),
                "source_label": source_label or None,
                "source_link": source_link or None,
                "summary_link": summary_link or None,
                "summary_text": summary_text or None,
                "hash": h,
                "time": datetime.fromisoformat(time_str),
            })
            seen_hashes.add(h)
            loaded += 1
        except Exception as e:
            log.warning(f"DB에서 손상된 행 발견, 건너뜀 (hash={h}): {e}")
            continue
    log.info(f"DB에서 최근 {DEDUP_WINDOW_MINUTES}분 내 기록 {loaded}/{len(rows)}건 불러옴")


def save_entry_to_db(entry: dict):
    db = get_db()
    params = (
        entry["hash"],
        entry["text"],
        json.dumps(list(entry["keywords"]), ensure_ascii=False),
        json.dumps(list(entry["numbers"])),
        json.dumps(list(entry["urls"]), ensure_ascii=False),
        entry.get("source_label") or "",
        entry.get("source_link") or "",
        entry.get("summary_link") or "",
        entry.get("summary_text") or "",
        entry["time"].isoformat(),
    )
    query = (
        "INSERT OR REPLACE INTO messages "
        "(hash, text, keywords, numbers, urls, source_label, source_link, summary_link, "
        "summary_text, time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    try:
        db.execute(query, params)
        db.commit()
    except sqlite3.DatabaseError as e:
        log.error(f"DB 저장 중 손상 감지({e}), 재연결 후 재시도")
        _invalidate_db_connection_on_corruption()
        db = get_db()
        db.execute(query, params)
        db.commit()


def update_entry_summary_link(hash_key: str, summary_link: str, summary_text: str = ""):
    """새 정보로 등록된 항목에, 요약방에 실제로 올라간 메시지의 링크와 요약본 텍스트를
    나중에 채워 넣음 (전송 성공 후에야 메시지 id/요약 결과를 알 수 있어서 등록 시점과 분리됨).
    이 요약본은 나중에 일일 정리(daily digest)를 만들 때 원문 대신 재사용됨(훨씬 짧아서 효율적)."""
    for entry in recent_buffer:
        if entry["hash"] == hash_key:
            entry["summary_link"] = summary_link
            if summary_text:
                entry["summary_text"] = summary_text
            save_entry_to_db(entry)
            return


def purge_old_entries_from_db(cutoff: datetime):
    db = get_db()
    db.execute("DELETE FROM messages WHERE time < ?", (cutoff.isoformat(),))
    db.execute("DELETE FROM duplicate_log WHERE time < ?", (cutoff.isoformat(),))
    db.commit()


def fetch_digest_rows(start_utc: datetime, end_utc: datetime):
    """정리(8시/18시) 생성에 쓰일 구간 내 메시지/중복로그를 조회."""
    db = get_db()
    rows = db.execute(
        "SELECT text, source_label, summary_text FROM messages WHERE time >= ? AND time < ? ORDER BY time",
        (start_utc.isoformat(), end_utc.isoformat()),
    ).fetchall()
    dup_rows = db.execute(
        "SELECT text, source_label FROM duplicate_log WHERE time >= ? AND time < ? ORDER BY time",
        (start_utc.isoformat(), end_utc.isoformat()),
    ).fetchall()
    return rows, dup_rows


def log_duplicate(text: str, source_label: str, source_link: str, matched_text: str):
    """중복으로 판정된 메시지를 가볍게 기록. 실시간 알림/향후 중복비교에는 전혀 관여하지 않고,
    오직 일일 정리(daily digest) 만들 때 안전망 자료로만 쓰임."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO duplicate_log (text, source_label, source_link, matched_preview, time) "
            "VALUES (?, ?, ?, ?, ?)",
            (text, source_label, source_link, make_title_preview(matched_text, 60), datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    except Exception as e:
        log.warning(f"중복 로그 저장 실패(무시하고 계속): {e}")


def _invalidate_db_connection_on_corruption():
    """이미 연결된 상태에서 DB 파일이 손상된 경우(예: 강제종료로 파일 자체가 깨짐), 캐시된
    연결 객체를 계속 재사용하면 매번 같은 예외가 반복됨. 이 함수를 호출하면 다음 get_db()
    호출 시 무결성 검사부터 다시 거쳐서(손상 감지 시 자동 백업+새 DB 생성) 복구를 시도함."""
    global _db_conn
    try:
        if _db_conn is not None:
            _db_conn.close()
    except Exception:
        pass
    _db_conn = None


def get_state(key: str) -> Optional[str]:
    db = get_db()
    try:
        row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.DatabaseError as e:
        log.error(f"DB 조회 중 손상 감지({e}), 재연결 시도")
        _invalidate_db_connection_on_corruption()
        db = get_db()
        row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def set_state(key: str, value: str):
    db = get_db()
    try:
        db.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (key, value))
        db.commit()
    except sqlite3.DatabaseError as e:
        log.error(f"DB 저장 중 손상 감지({e}), 재연결 후 재시도")
        _invalidate_db_connection_on_corruption()
        db = get_db()
        db.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (key, value))
        db.commit()


def get_last_processed_id(chat_id) -> Optional[int]:
    """이 채널에서 마지막으로(성공/실패/잡담/중복 여부와 무관하게) 처리를 시도했던 메시지
    ID. 서버 중단 후 재시작 시, 이 지점 이후의 메시지만 다시 훑으면 되도록 워터마크 역할."""
    val = get_state(f"last_msg_id:{chat_id}")
    return int(val) if val else None


def set_last_processed_id(chat_id, message_id: int):
    set_state(f"last_msg_id:{chat_id}", str(message_id))


def pin_bot_message(chat_id, message_id: int) -> bool:
    """봇 명의로 메시지를 채널에 고정."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/pinChatMessage",
            json={"chat_id": chat_id, "message_id": message_id, "disable_notification": True},
            timeout=15,
        )
        if resp.status_code == 200:
            return True
        log.warning(f"메시지 고정 실패: {resp.status_code} {resp.text}")
    except Exception as e:
        log.warning(f"메시지 고정 중 오류: {e}")
    return False


def unpin_bot_message(chat_id, message_id: int) -> bool:
    """봇 명의로 채널에서 특정 고정 메시지를 해제."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/unpinChatMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=15,
        )
        if resp.status_code == 200:
            return True
        log.warning(f"메시지 고정 해제 실패: {resp.status_code} {resp.text}")
    except Exception as e:
        log.warning(f"메시지 고정 해제 중 오류: {e}")
    return False


MAX_SEND_QUEUE_ATTEMPTS = 20  # 이 횟수만큼 재시도해도 실패하면 포기하고 큐에서 제거


def queue_failed_send(chat_id, text: str):
    """전송 실패한 텍스트 메시지를 나중에 재시도할 수 있도록 큐에 저장."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO send_queue (chat_id, text, attempts, created_at, kind) VALUES (?, ?, 0, ?, 'text')",
            (str(chat_id), text, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    except Exception as e:
        log.warning(f"재전송 큐 저장 실패(무시하고 계속): {e}")


def queue_failed_media_send(chat_id, caption: str, source_chat_id, source_message_id: int):
    """전송 실패한 이미지/미디어 메시지를, 원본 위치(어느 채널의 몇 번 메시지)를 기억해뒀다가
    나중에 다시 가져와서 재전송할 수 있도록 큐에 저장."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO send_queue (chat_id, text, attempts, created_at, kind, source_chat_id, source_message_id) "
            "VALUES (?, ?, 0, ?, 'media', ?, ?)",
            (str(chat_id), caption, datetime.now(timezone.utc).isoformat(), str(source_chat_id), source_message_id),
        )
        db.commit()
    except Exception as e:
        log.warning(f"미디어 재전송 큐 저장 실패(무시하고 계속): {e}")


def get_pending_sends(limit: int = 20) -> list:
    db = get_db()
    return db.execute(
        "SELECT id, chat_id, text, attempts, kind, source_chat_id, source_message_id "
        "FROM send_queue ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


def remove_from_send_queue(row_id: int):
    db = get_db()
    db.execute("DELETE FROM send_queue WHERE id = ?", (row_id,))
    db.commit()


def bump_send_queue_attempts(row_id: int):
    db = get_db()
    db.execute("UPDATE send_queue SET attempts = attempts + 1 WHERE id = ?", (row_id,))
    db.commit()

STOPWORDS = {
    "그리고", "그래서", "합니다", "입니다", "있습니다", "했습니다", "하는", "있는",
    "오늘", "지금", "여러분", "구독", "채널", "링크", "공지", "안내", "감사합니다",
    # 자동 발신 경제지표 봇/뉴스 저작권 틀에서 반복되는 문구 (실제 내용과 무관하게
    # 매번 똑같이 등장해서, 그대로 두면 서로 다른 지표/기사도 유사도가 높게 나오는 원인이 됨)
    "경제지표", "지표", "스펙", "국가", "지표명", "주기", "소스", "데이터", "대상",
    "기간", "실제치", "예상치", "예측치", "직전치", "저작권자", "연합인포맥스", "연합인포",
    "무단", "전재", "재배포", "금지", "학습", "활용", "ai", "기자", "무단전재",
    # DART(전자공시) 자동 발신 봇 틀 문구
    "기업명", "보고서명", "공시링크", "회사정보", "시가총액", "dart", "fss", "finance",
    "naver", "item", "main", "nhn", "code", "rcpno",
    # 매 게시물 맨 앞에 채널명이 그대로 반복되는 채널들 (내용과 무관하게 유사도를 부풀리는 원인)
    "주식소리통", "bilanx", "neo",
    # 이데일리FX/데이터투자 자동생성 실적 리포트 틀 문구 (AI 생성 안내, 법적 고지 등이 회사와
    # 무관하게 매번 그대로 반복돼서, 완전히 다른 두 회사의 실적 발표도 유사도가 높게 나오는 원인)
    "이데일리fx", "이데일리", "데이터투자", "공시팀", "생성한", "번역", "과정에서", "문맥상",
    "오류가", "포함될", "판단의", "본인에게", "결정의", "근거로", "단독", "활용하지", "주의하시기",
    "전문기관인", "작성한", "정보로", "결과에", "콘텐츠는", "논조", "편집", "방향과", "연락하시기",
    "경제정보", "미디어", "콘텐츠", "종합", "외부", "문의는",
    "책임은", "않도록", "있으며", "자료를", "바랍니다", "책임", "다를", "참고", "작성", "제목",
    "nyse", "최종",
    # 키움증권 리서치 채널의 매 게시물 하단에 반복되는 앱 홍보/안내 문구
    "키움증권", "박기현", "영웅문s", "해외주식", "리서치", "많은", "보고싶으신", "분은",
    "접속하셔서", "메뉴에서", "메뉴", "확인하세요", "단말기에서", "단말기에서만", "설치된",
    # 시장 시간대 단어(프리마켓, 장마감 등)는 원래 잡담 판별용으로 넣었던 건데, 그 판별
    # 로직 자체를 없애면서 지금은 유사도만 부풀리는 역할만 남음 (예: 완전히 다른 종목의
    # "OO 프리마켓 X% 하락" 캡션끼리 "프리마켓"만 겹쳐서 오탐되는 문제)
    "프리마켓", "장마감", "개장", "마감시황", "장중", "폐장", "시간외",
    # 여러 채널의 정기 다이제스트/브리핑 제목에 반복되는 템플릿 문구
    "브리핑", "수집", "칼럼",
}

URL_RE = re.compile(r"https?://\S+")
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
TOKEN_RE = re.compile(r"[가-힣\u4e00-\u9fffA-Za-z0-9]{2,}")


def make_preview(text: str, length: int) -> str:
    """로그/알림에 쓸 짧은 미리보기 생성. 줄바꿈/탭/연속 공백을 전부 단일 공백으로 정리한 뒤
    자르기 때문에, 원본에 공백이 많이 섞여있어도 텔레그램에서 줄바꿈된 것처럼 안 보임."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed[:length].rstrip()


def make_title_preview(text: str, length: int) -> str:
    """'제목'으로 보여줄 미리보기 생성. URL을 먼저 제거해서, 링크가 앞부분에 길게 있어도
    실제 캡션/제목 내용이 우선적으로 보이게 함. 캡션 없이 링크만 있던 메시지라면
    (제거하고 나니 내용이 없으면) 그 URL 자체를 대신 보여줌."""
    without_urls = URL_RE.sub("", text)
    without_urls = strip_title_prefix(without_urls)
    preview = make_preview(without_urls, length)
    if not preview:
        urls_found = URL_RE.findall(text)
        preview = _strip_trailing_url_punctuation(urls_found[0]) if urls_found else make_preview(text, length)
    return preview


def strip_title_prefix(text: str) -> str:
    """원본 메시지가 이미 '제목 : ...' 형태로 시작하는 경우, 그 접두어를 제거.
    우리 쪽 알림 템플릿에서 자체적으로 '제목:' 라벨을 붙일 때 중복(제목: 제목: ...)되는 걸 방지."""
    return re.sub(r"^\s*제목\s*[:：]\s*", "", text)


def build_summary_link(message_id: Optional[int]) -> Optional[str]:
    """요약방(DIGEST_CHAT)에 실제로 올라간 메시지로 바로 가는 링크를 만듦."""
    if not message_id:
        return None
    chat_str = str(DIGEST_CHAT)
    if chat_str.startswith("-100"):
        return f"https://t.me/c/{chat_str[4:]}/{message_id}"
    # username 형태(공개 채널)로 설정된 경우
    return f"https://t.me/{str(DIGEST_CHAT).lstrip('@')}/{message_id}"


def normalize(text: str) -> str:
    text = URL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


def normalize_url(raw_url: str) -> str:
    """추적 파라미터(쿼리/프래그먼트)를 떼고 '도메인+경로'만 남겨 같은 기사인지 비교 가능하게 만듦."""
    try:
        parts = urlsplit(raw_url)
        host = parts.netloc.lower().removeprefix("www.")
        path = parts.path.rstrip("/")
        return f"{host}{path}"
    except Exception:
        return raw_url.strip().lower()


def exact_hash(normalized_text: str) -> str:
    return hashlib.md5(normalized_text.encode("utf-8")).hexdigest()


TRAILING_URL_PUNCTUATION = ")]}.,;:!?\"'”’》〉」』"


def _strip_trailing_url_punctuation(url: str) -> str:
    """'(by https://t.me/xyz)'처럼 URL 뒤에 문장부호가 바로 붙어있으면, URL_RE가 그
    문장부호까지 URL의 일부로 잘못 포함시켜버림 (예: 'https://t.me/xyz)'). 이러면 같은
    채널의 서로 다른 두 게시물이 이 잘못된 문자열을 '같은 URL'로 오인해서 중복 처리되는
    심각한 오탐이 생기므로, 끝에 붙은 문장부호를 제거함. 다만 여는 괄호가 URL 안에 있으면
    (드물게 URL 자체에 괄호가 포함되는 경우) 닫는 괄호는 지우지 않고 그대로 둠."""
    while url and url[-1] in TRAILING_URL_PUNCTUATION:
        if url[-1] == ")" and url.count("(") > url.count(")"):
            break  # URL 안에 정당하게 포함된 괄호일 수 있으므로 보존
        url = url[:-1]
    return url


def _is_generic_telegram_channel_link(url: str) -> bool:
    """'t.me/채널명'처럼 특정 게시물이 아니라 채널 자체를 가리키는 링크인지 확인.
    많은 채널이 매 게시물 끝에 자기 채널 홍보 링크(예: 't.me/davidstocknew')를 서명처럼
    붙이는데, 이건 그 채널의 '모든' 게시물에 똑같이 등장해서 그대로 두면 완전히 다른 두
    게시물이 '같은 URL을 공유한다'고 오인되는 원인이 됨. 반면 't.me/채널명/12345'처럼
    메시지 ID까지 포함된 링크는 특정 게시물을 정확히 가리키므로 제외하지 않음."""
    return bool(re.fullmatch(r"t\.me/[A-Za-z0-9_]+/?", url))


def extract_urls(original_text: str) -> set:
    urls = {normalize_url(_strip_trailing_url_punctuation(u)) for u in URL_RE.findall(original_text)}
    return {u for u in urls if not _is_generic_telegram_channel_link(u)}


# ── 잡담(주식과 무관한 대화) 필터링용 ──
STOCK_SIGNAL_KEYWORDS = {
    "매수", "매도", "목표가", "목표주가", "손절", "익절", "진입", "청산", "관심종목",
    "상한가", "하한가", "급등", "급락", "공매도", "코스피", "코스닥", "나스닥", "다우",
    "환율", "리포트", "실적", "배당", "유상증자", "무상증자", "상장", "공모", "IPO",
    "ETF", "선물", "옵션", "전망", "차트", "지지선", "저항선", "시가총액", "거래량",
    "주가", "종목", "매매", "포지션", "롱", "숏", "레버리지",
    # 시황에 영향을 주는 정치/거시경제 뉴스도 주식 관련 정보로 취급
    "금리", "연준", "fomc", "관세", "제재", "무역전쟁", "대통령", "정부", "국회",
    "정책", "규제", "총선", "대선", "전쟁", "휴전", "협상", "무역", "gdp", "물가",
    "인플레이션", "고용지표", "실업률", "중앙은행", "재무부",
    # 원자재/에너지 (유가 등은 에너지·화학·항공주 등에 직접 영향을 주는 핵심 지표)
    "유가", "원유", "wti", "브렌트유", "천연가스", "금값", "은값", "구리", "니켈",
    "원자재", "산유국", "opec",
    # 시장 시간대/차트 관련 (짧은 단어 하나만 캡션으로 붙는 경우가 많아 별도로 명시)
    "프리마켓", "장마감", "개장", "마감시황", "히트맵", "장중", "폐장", "시간외",
}
FINANCE_URL_DOMAINS = {
    "yna.co.kr", "hankyung.com", "mk.co.kr", "wsj.com", "bloomberg.com", "reuters.com",
    "investing.com", "finance.yahoo.com", "sedaily.com", "fnnews.com", "edaily.co.kr",
    "biz.chosun.com", "hankyung.co.kr", "asiae.co.kr", "moneys.co.kr",
}

# ── 투자 정보 알림에 붙일 이모티콘 자동 선택용 키워드 ──
NON_STOCK_CONTEXT_KEYWORDS = {
    "당근", "당근마켓", "중고나라", "중고", "직거래", "번개장터", "택배거래", "새상품",
    "무료나눔", "나눔합니다", "구매합니다", "판매합니다", "팔아요", "삽니다",
}


NEWS_ARTICLE_SIGNALS = [
    "저작권자", "무단전재", "무단 전재", "재배포 금지", "재배포금지", "무단 배포", "무단배포",
]


TICKER_HASHTAG_RE = re.compile(r"[#$][A-Za-z]{1,5}\b")


KEYWORD_FALSE_POSITIVE_COMPOUNDS = [
    "반대매매",  # '매매' 키워드가 이 단어 안에 우연히 포함돼서, 진짜 매매 정보가 아닌
    # 문장(예: 반대매매로 인한 손실 관련 개인적 소회)도 자동 통과되는 걸 막기 위함
]


def looks_stock_related_heuristic(text: str, urls: set) -> Optional[bool]:
    """빠른 1차 판단. True/False면 확실한 경우, None이면 애매해서 LLM 필요.
    주의: 예전엔 STOCK_SIGNAL_KEYWORDS 매칭만으로도 자동 통과시켰는데, '종목기도회'(패러디
    영상)의 '종목', '실적 발표' 여러 개가 나열된 밈 카드뉴스의 '실적'처럼 키워드가 우연히
    포함된 경우를 계속 놓쳐서 없앰. 이제 '확실한 신호'만 자동 처리하고, 그 외 애매한 건
    전부 LLM이 맥락까지 보고 판단하게 함 (비용은 조금 늘지만 정확도가 훨씬 중요함)."""
    if any(any(domain in u for domain in FINANCE_URL_DOMAINS) for u in urls):
        return True
    # 저작권 문구("저작권자", "무단전재 및 재배포 금지" 등)가 있으면 정식 언론사 기사라는 뜻이라,
    # 주제가 뭐든(CSR, 소비자 안전, 사건사고 등) 채널이 골라서 전달한 뉴스이므로 통과시킴.
    # 이런 문구는 당근마켓 등 개인 잡담에는 절대 안 나오니 별도 예외 체크 없이 바로 신뢰 가능.
    if any(marker in text for marker in NEWS_ARTICLE_SIGNALS):
        return True
    # '#MU', '$AAPL'처럼 해시태그/캐시태그 형태의 종목코드가 있으면 명백히 주식 관련
    # (짧은 캡션이라 아래 길이 체크에 걸려 잡담으로 오판되는 걸 방지)
    if TICKER_HASHTAG_RE.search(text):
        return True
    stripped = text.strip()
    # 아주 짧고 숫자도 없는 메시지("ㅋㅋ", "감사합니다" 등)는 잡담일 확률이 매우 높음
    if len(stripped) <= 8 and not NUMBER_RE.search(stripped) and not urls:
        return False
    return None  # 애매함 → LLM 재확인


def classify_image_is_stock_related(image_bytes: bytes, media_type: str = "image/jpeg") -> Optional[bool]:
    """캡션이 없는 이미지 메시지는 텍스트로 판단할 게 없어서 여태까지 무조건 통과시켰는데,
    밈/캐릭터 삽화 같은 순수 잡담 이미지까지 다 새 정보로 나가는 문제가 있었음.
    이미지 내용을 직접(비전) 봐서 주식/투자 관련인지 판단. Anthropic API 필요.
    판단 불가능한 상황(API 키 없음, 호출 실패 등)이면 None 반환 → 안전하게 통과시킴."""
    if not anthropic_client:
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        resp = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {
                        "type": "text",
                        "text": (
                            "이 이미지가 주식/투자 관련 정보(차트, 시황, 종목 분석, 공시, 뉴스 캡처, "
                            "지표 등)인지, 아니면 밈/캐릭터 삽화/농담 같은 잡담용 이미지인지 판단해줘. "
                            "답은 반드시 STOCK 또는 CHITCHAT 한 단어로만 답해."
                        ),
                    },
                ],
            }],
        )
        answer = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip().upper()
        return "CHITCHAT" not in answer
    except Exception as e:
        log.warning(f"이미지 잡담 판단 실패, 안전하게 통과시킴: {e}")
        return None


def classify_is_stock_related(text: str, urls: set) -> str:
    """'STOCK', 'CHITCHAT', 'OPINION' 중 하나를 반환.
    OPINION은 실행 가능한 정보는 없지만 시장/정책 등에 대한 개인의 분석적 의견/소신 발언인 경우
    (일반 잡담과 구분해서 '잡담(의견)'으로 표시하는 데 씀)."""
    heuristic = looks_stock_related_heuristic(text, urls)
    if heuristic is True:
        return "STOCK"
    if heuristic is False:
        return "CHITCHAT"
    prompt = (
        "아래 텔레그램 메시지가 'STOCK'(주식/투자 관련 정보), 'CHITCHAT'(주식과 무관한 잡담), "
        "'OPINION'(실행 가능한 정보는 없지만 시장/정책 등에 대한 개인의 분석적 의견이나 소신 "
        "발언) 중 어디에 해당하는지 판단해줘.\n"
        "종목, 매매 방향, 가격/차트, 시황 뿐 아니라 금리, 관세, 전쟁, 정상회담, 선거처럼 "
        "주가나 시장에 영향을 줄 수 있는 정치/거시경제 뉴스도 STOCK으로 간주해줘.\n"
        "인사말, 감사 인사, 채널 홍보, 순수 잡담, 농담처럼 시장과 무관한 대화는 CHITCHAT이야.\n"
        "중요: 시장 관련 단어(베어마켓, 급등, 폭락 등)를 언급하더라도, 그 자체로 새로운 정보나 "
        "분석을 전달하지 않고 다른 메시지에 대한 짧은 반응/감상/맞장구만 담은 경우도 CHITCHAT으로 "
        "판단해. 예: '대놓고 베어마켓이라고 쓰는구나', 'ㄹㅇㅋㅋ 이러다 진짜 되겠는데' 같은 문장은 "
        "그 자체로 독립적인 정보가 없으므로 CHITCHAT이야.\n"
        "또한, 특정 종목명·가격·방향성 등 구체적이고 실행 가능한 정보 없이 "
        "'오를 건 오르고 내릴 건 내린다', '요즘 장이 왜 이러냐' 처럼 막연한 시장 한탄/감상만 "
        "담은 문장도 CHITCHAT으로 판단해.\n"
        "또한 중요: 실제 데이터나 근거 없이, 누군가 미리 정보를 알고 움직이는 것 같다는 식의 "
        "막연한 추측·의심·농담 섞인 코멘트도 CHITCHAT이야. 예: '내일 CPI 잘 나오는 거 미리 확인하고 "
        "칼춤 추는 중일지....'는 CPI라는 단어가 있지만 실제 CPI 전망치나 데이터는 전혀 없고, "
        "'누가 선행매매 하는 거 아니냐'는 슬랭 섞인 농담 섞인 추측일 뿐이므로 CHITCHAT이야. 이런 "
        "문장을 근거로 '투자자들이 CPI가 예상치보다 낮게 나올 가능성을 선반영해 매수/매도 대응 "
        "중이다'처럼 실제로 언급되지 않은 구체적 내용을 지어내면 안 돼.\n"
        "또한 중요: 내일/오늘 있을 행사·토론회·발표 등을 짧게 알리면서 'ㅋㅋㅋ' 같은 웃음/냉소 "
        "반응이 붙어 있으면, 정식 보도가 아니라 그 자체가 비웃음/냉소적 반응이므로 CHITCHAT이야. "
        "예: '내일 초과이윤 재분배 토론회 개최됩니다. ㅋㅋㅋㅋ'는 실제 사건을 언급하지만 웃음 "
        "반응이 붙어있어 냉소적 코멘트일 뿐이므로 CHITCHAT이야.\n"
        "또한 중요: 여러 회사/종목명이 나열되고 '실적 발표' 같은 단어가 반복되더라도, 형식이 "
        "'9가지 조건이 전부 맞아야 한다'는 식의 과장된 밈/유머 시나리오이거나 비속어(예: '아가리 "
        "싸물기', '징징대는')가 섞여 있으면 진지한 분석이 아니라 밈/유머이므로 CHITCHAT이야.\n"
        "또한 중요: '시장', '손실', '반대매매', '투자' 같은 단어가 여러 번 나오고 길게 쓰여 있어도, "
        "종목명·가격·매매 방향·구체적 분석 같은 실행 가능한 정보가 전혀 없이 운영자의 개인적인 "
        "소회, 다른 채널 운영자/구독자들에 대한 응원·위로, 힘든 시장 상황에 대한 감정적 반응만 "
        "담겨 있다면 CHITCHAT이야. 길이가 길고 진지한 어조라고 해서 정보성 글이 되는 건 아니야.\n"
        "또한 중요: 실행 가능한 정보(종목/가격/매매방향)는 없지만, 시장 구조나 정책·정부 발언 등이 "
        "시장에 미치는 영향에 대해 나름의 논리를 갖추고 분석적으로 서술한 개인 의견/소신 발언이면 "
        "OPINION이야. 예: '한국 메모리 비중이 커지면서 정부 인사 발언이 블룸버그를 통해 재귀적으로 "
        "시장에 영향을 주는 것 같다'처럼 자기 나름의 분석 논리를 전개하는 글은 OPINION이야. "
        "짧은 문장이라도, 경제 이론이나 개념을 빌려와 현재 상황을 평가하는 개인적 코멘트도 OPINION "
        "이야. 예: '개인적으로 이번 장은 맨큐의 경제학원론 같은데서 정부실패의 대표적인 케이스로 "
        "다뤄도 된다고 생각함'은 구체적 종목/가격 정보가 전혀 없지만, 경제학 개념(정부실패)을 빌려 "
        "정책 상황을 평가하는 개인 의견이므로 OPINION이야 (STOCK으로 오인해서 요약하면 안 됨).\n"
        "단순 반응/한탄(위 CHITCHAT 규칙들)과 달리, 논리적 근거를 갖춘 주장이면 OPINION으로 구분해.\n"
        "또한 중요: '매수', '매도', '가격' 같은 단어가 있어도, 당근마켓/중고나라 등 중고물품 거래나 "
        "개인적인 일상 대화 맥락이면 CHITCHAT이야. 예: '노트북 매수 문의가 오네요, 20만원에 팔고 "
        "올게요'는 '매수'라는 단어가 있지만 실제로는 중고 노트북 판매 잡담이므로 CHITCHAT이야. "
        "단어만 보지 말고 실제로 주식/투자를 이야기하는 맥락인지 확인해.\n"
        "또한 중요: 특정 회사/기업을 주체로 한 뉴스 기사라면, 사회공헌(CSR)·인사이동·조직개편·수상 "
        "소식처럼 시장에 직접적인 영향이 없는 내용이라도 STOCK으로 간주해. 예: '한국자금중개, "
        "경희궁 환경정화 봉사'처럼 특정 상장/비상장 기업이 주어인 뉴스는 시황과 무관해도 STOCK이야. "
        "다만 이건 정식 언론사가 그 기업의 경영활동을 보도하는 '기업을 주체로 한 뉴스 기사'일 때만 "
        "해당해. 개인 잡담/인사말/채널 홍보/중고거래 뿐 아니라, 개인 블로그의 취미·상품 후기· "
        "장난감/캐릭터 리뷰 등도 여전히 CHITCHAT이야 — 그 안에 마트/기업 이름이나 '매출', '순위' "
        "같은 단어가 우연히 섞여 있어도(예: 장난감 인기순위 표에 '롯데마트 매출' 같은 문구가 "
        "곁들여진 경우), 글의 주제 자체가 기업 경영이 아니라 개인 취미/제품 리뷰라면 CHITCHAT이야.\n"
        "답은 반드시 STOCK, CHITCHAT, OPINION 중 하나만 답해.\n\n"
        f"[메시지]\n{text}"
    )
    answer = call_llm(prompt, max_tokens=5)
    if answer is None:
        # 판단 실패 시, 놓치는 것보다 안전하게 통과시키는 쪽을 택함
        return "STOCK"
    answer = answer.strip().upper()
    if answer.startswith("OPINION"):
        return "OPINION"
    if answer.startswith("STOCK"):
        return "STOCK"
    return "CHITCHAT"


# 흔한 한국어 조사(은/는/이/가/을/를/으로 등). 길이가 긴 것부터 검사해야
# '이라며' 같은 복합 조사가 '며' 등으로 잘못 짧게 잘리는 걸 방지함.
KOREAN_PARTICLE_SUFFIXES = sorted([
    "으로는", "에서는", "에게는", "이라며", "다며", "이라고", "다고",
    "으로", "에서", "에게", "부터", "까지", "한테", "이지만", "지만",
    "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만",
], key=len, reverse=True)


def strip_korean_particle(token: str) -> str:
    """토큰 끝에 붙은 흔한 조사를 제거. '사우디가' -> '사우디', '공격으로' -> '공격'처럼
    같은 단어라도 조사 때문에 다른 토큰으로 인식되어 유사도 매칭을 놓치는 걸 방지.
    너무 짧아지는 경우(의미 없는 글자만 남음)는 원본을 그대로 둠."""
    for suf in KOREAN_PARTICLE_SUFFIXES:
        if token.endswith(suf) and len(token) - len(suf) >= 2:
            return token[: -len(suf)]
    return token


def extract_signature(normalized_text: str):
    """키워드 집합과 숫자(가격/수량 등) 집합을 추출. (URL은 별도로 extract_urls 사용)"""
    tokens = TOKEN_RE.findall(normalized_text)
    keywords = {t for t in tokens if t not in STOPWORDS and not t.isdigit()}
    # 조사 뗀 버전도 같이 넣어줌 (원본은 유지 - 혹시 모를 오탈자 방지, 합집합이라 손해 없음)
    stripped = {strip_korean_particle(t) for t in keywords}
    keywords |= {t for t in stripped if t not in STOPWORDS and not t.isdigit()}
    numbers = set()
    for m in NUMBER_RE.findall(normalized_text):
        try:
            numbers.add(float(m.replace(",", "")))
        except ValueError:
            pass
    return keywords, numbers


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def containment(a: set, b: set) -> float:
    """짧은 캡션 vs 긴 문단처럼 길이 차이가 큰 두 텍스트를 비교할 때, 자카드 유사도만으로는
    짧은 쪽 단어가 긴 쪽에 거의 다 포함돼 있어도 점수가 희석되는 문제가 있음.
    이걸 보완하기 위해 '더 작은 집합의 단어들이 더 큰 집합에 얼마나 포함되는지' 비율을 계산."""
    if not a or not b:
        return 0.0
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    return len(smaller & larger) / len(smaller)



# 두 숫자를 '같은 값'으로 볼 오차 허용치. 예전엔 3%였는데, 환율/시세처럼 계속 조금씩
# 움직이는 숫자에는 3%가 너무 느슨함 (예: 1,489원과 1,494원은 0.34% 차이인데, 이는 하루
# 안에도 자연스럽게 발생하는 변동폭이라 서로 다른 시점의 값인데도 "같은 값"으로 오판됨).
# 원래 이 로직이 의도했던 건 '같은 값의 반올림 차이'(예: 4,005.70 vs 4,005.7) 정도라,
# 훨씬 타이트하게 잡아도 그 목적은 충분히 달성됨.
NUMBER_MATCH_TOLERANCE = float(os.environ.get("NUMBER_MATCH_TOLERANCE", 0.001))


def _looks_like_calendar_number(n: float) -> bool:
    """1~31 사이의 '정수'만 날짜(요일/월)로 간주. 소수점이 있는 값(예: 27.29%)은 날짜일
    수 없으므로 제외 대상에서 뺌 (정상적인 퍼센트/가격 수치까지 걸러지는 걸 방지)."""
    return n == int(n) and 1 <= n <= 31


def number_overlap_score(a: set, b: set) -> float:
    """두 숫자 집합에 NUMBER_MATCH_TOLERANCE(기본 0.1%) 오차 내로 겹치는 값이 있으면 1.0,
    없으면 0.0.
    단, 연도로 보이는 숫자(1900~2100)와 날짜(요일/월 등)로 흔히 쓰이는 1~31 사이의 정수는
    비교 대상에서 제외함. 서로 다른 연도(예: 2026 vs 2027)나, '7/20'과 '07월 20일'처럼
    같은 주에 나온 서로 다른 리포트가 우연히 같은 날짜를 언급한 경우도 숫자 자체는 일치하지만
    실제로 같은 데이터를 가리키는 게 아님. 이런 우연한 숫자 일치 때문에 완전히 무관한 두 문서
    (예: 중국 돼지고기 가격 뉴스 vs TSMC 가격 인상 뉴스, 서로 다른 애널리스트의 주간 리포트
    두 개)가 '같은 수치를 공유한다'고 오판되는 원인이었음."""
    if not a or not b:
        return 0.0
    for x in a:
        if 1900 <= x <= 2100 or _looks_like_calendar_number(x):
            continue  # 연도 또는 날짜(요일/월)로 보이는 정수는 건너뜀
        for y in b:
            if 1900 <= y <= 2100 or _looks_like_calendar_number(y):
                continue
            if x == 0 and y == 0:
                return 1.0
            if x == 0 or y == 0:
                continue
            if abs(x - y) / max(abs(x), abs(y)) <= NUMBER_MATCH_TOLERANCE:
                return 1.0
    return 0.0


MIN_KW_SCORE_FOR_NUMBER_BOOST = 0.15


def similarity_score(kw1, num1, kw2, num2) -> float:
    kw_score = jaccard(kw1, kw2)
    smaller_size = min(len(kw1), len(kw2))
    if smaller_size >= 3:
        # 짧은 캡션과 긴 문단처럼 길이 차이가 클 때, 자카드로는 놓칠 수 있는 "포함 관계"도
        # 같이 확인. 자카드보다 살짝 낮게 반영해서 우연한 소수 단어 겹침에 과도하게 반응하지 않게 함
        kw_score = max(kw_score, containment(kw1, kw2) * 0.9)

    num_score = number_overlap_score(num1, num2)
    if kw_score < MIN_KW_SCORE_FOR_NUMBER_BOOST:
        # 키워드가 사실상 거의 안 겹치는(=주제가 무관한) 두 메시지는, 숫자 하나가 우연히
        # 일치한다고 해서 유사도를 인위적으로 끌어올리면 안 됨 (예: 완전히 다른 두 리포트에서
        # 각자 "1.5%"라는 흔한 수치가 우연히 겹치는 경우). 최소한의 주제 연관성(키워드 겹침)이
        # 있을 때만 숫자 일치를 추가 근거로 인정함.
        num_score = 0.0

    return 0.7 * kw_score + 0.3 * num_score


def _call_anthropic(prompt: str, max_tokens: int) -> str:
    _throttle_llm_calls()
    resp = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    ).strip()


def _call_openai(prompt: str, max_tokens: int) -> str:
    _throttle_llm_calls()
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={
            "model": OPENAI_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


_gemini_key_counter = 0


def _gemini_keys_rotated() -> list:
    """호출마다 시작 키를 하나씩 밀어서(라운드로빈) 부하를 여러 키에 고르게 분산."""
    global _gemini_key_counter
    if not GEMINI_API_KEYS:
        return []
    start = _gemini_key_counter % len(GEMINI_API_KEYS)
    _gemini_key_counter += 1
    return GEMINI_API_KEYS[start:] + GEMINI_API_KEYS[:start]


# 키 하나가 429를 받아야만 다음 키로 넘어가는 '사후 대응' 방식은, 아직 회복 안 된 키를
# 매번 다시 두들겨서 요청만 낭비하고 429가 영원히 안 풀리는 문제가 있었음. 이제 키별로
# 최근 사용 시각을 직접 추적해서, 아직 자기 몫의 분당 한도가 안 찬 키만 실제로 사용함.
GEMINI_PER_KEY_RPM = int(os.environ.get("GEMINI_PER_KEY_RPM", 12))  # 무료티어 15RPM보다 살짝 낮게 여유
# 모든 키가 소진됐을 때 최후 수단으로 기다리는 시간의 상한(초). 이보다 길게 기다려야 하면
# 아예 기다리지 않고 바로 실패 처리함 (채팅이 몰아칠 때 공유 락을 오래 붙잡아 대기열이
# 눈덩이처럼 불어나는 걸 방지하기 위함).
GEMINI_MAX_COOLDOWN_WAIT = float(os.environ.get("GEMINI_MAX_COOLDOWN_WAIT", 5))
_gemini_key_recent_calls: dict = {}  # key -> [최근 호출 시각, ...]


def _gemini_key_wait_seconds(key: str) -> float:
    """이 키를 지금 써도 되면 0.0, 아직 자기 몫의 분당 한도가 찼으면 몇 초 더 기다려야
    하는지 반환."""
    now = time.time()
    recent = [t for t in _gemini_key_recent_calls.get(key, []) if now - t < 60]
    _gemini_key_recent_calls[key] = recent
    if len(recent) < GEMINI_PER_KEY_RPM:
        return 0.0
    return 60 - (now - recent[0]) + 0.1


def _mark_gemini_key_used(key: str):
    _gemini_key_recent_calls.setdefault(key, []).append(time.time())


def _mark_gemini_key_exhausted(key: str):
    """실제로 429를 받으면, 우리 쪽 추적이 틀렸다는 게 증명된 것임 (예: 방금 재시작해서
    로컬 기록은 비어있지만 Google 서버 쪽 한도는 재시작해도 안 풀리는 경우). 이럴 땐 우리
    기록을 신뢰하지 말고, 이 키를 강제로 '방금 한도를 다 썼다'고 기록해서 앞으로 60초간은
    다시 건드리지 않도록 함."""
    now = time.time()
    _gemini_key_recent_calls[key] = [now] * GEMINI_PER_KEY_RPM


def _call_gemini(prompt: str, max_tokens: int) -> str:
    keys = _gemini_keys_rotated()
    if not keys:
        raise RuntimeError("GEMINI_API_KEY(S) 없음")

    # 1차 시도: 지금 당장 여유가 있는(자기 몫의 분당 한도가 안 찬) 키만 순서대로 시도.
    # 여유 없는 키는 건드리지도 않고 건너뛰어서, 이미 지친 키를 계속 두들기는 낭비를 방지함.
    last_error = None
    on_cooldown = []  # (key, 남은 대기초) — 전부 여유 없을 때 최후 수단으로 씀
    for idx, key in enumerate(keys):
        wait = _gemini_key_wait_seconds(key)
        if wait > 0:
            on_cooldown.append((key, wait))
            log.info(f"Gemini 키 #{idx+1}는 아직 분당 한도가 안 풀려서 건너뜀 (약 {wait:.1f}초 남음)")
            continue
        try:
            _throttle_llm_calls()
            _mark_gemini_key_used(key)
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                params={"key": key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens},
                },
                timeout=30,
            )
            resp.raise_for_status()
            if idx > 0:
                log.info(f"Gemini 키 {idx+1}번째로 전환해서 호출 성공")
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            last_error = e
            _mark_gemini_key_exhausted(key)
            log.info(f"Gemini 키 #{idx+1} 호출 실패, 다음 키 시도: {e}")
            continue

    # 모든 키가 당장은 여유가 없는 상태 → 가장 빨리 회복되는 키 하나만 골라서, 그만큼만
    # 기다렸다가 마지막으로 한 번 시도 (9개를 전부 헛되이 두들기지 않고 최소한으로 대기).
    # 단, 이 호출은 llm_processing_lock을 쥔 채로 실행되기 때문에, 대기 시간이 길어지면
    # 뒤에 밀린 다른 메시지들이 전부 락을 기다리며 줄줄이 쌓이게 됨. 채팅이 몰아칠 때 이게
    # 누적되면 대기열이 눈덩이처럼 불어나 메모리 압박으로 프로세스가 죽는 원인이 됐음
    # (실제로 겪은 장애). 그래서 대기 시간에 짧은 상한을 두고, 그보다 오래 걸리면 기다리지
    # 않고 바로 실패시켜서 상위 폴백(다른 provider 시도 또는 '다른 정보'로 안전 처리)으로
    # 빠르게 넘어가도록 함.
    if on_cooldown:
        soonest_key, soonest_wait = min(on_cooldown, key=lambda x: x[1])
        if soonest_wait > GEMINI_MAX_COOLDOWN_WAIT:
            log.info(
                f"Gemini 키 전부 분당 한도 도달, 가장 빨리 회복되는 키까지 {soonest_wait:.1f}초"
                f"(상한 {GEMINI_MAX_COOLDOWN_WAIT}초 초과) → 대기 없이 바로 실패 처리"
            )
            raise last_error or RuntimeError(
                f"Gemini 키 전부 분당 한도 도달 (가장 빨리 회복되는 키까지 {soonest_wait:.1f}초, "
                f"상한 {GEMINI_MAX_COOLDOWN_WAIT}초 초과)"
            )
        log.info(f"Gemini 키 전부 분당 한도 도달 → 가장 빨리 회복되는 키까지 {soonest_wait:.1f}초 대기")
        time.sleep(soonest_wait)
        try:
            _throttle_llm_calls()
            _mark_gemini_key_used(soonest_key)
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                params={"key": soonest_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens},
                },
                timeout=30,
            )
            resp.raise_for_status()
            log.info("대기 후 재시도 성공")
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            last_error = e
            _mark_gemini_key_exhausted(soonest_key)
            log.info(f"대기 후 재시도도 실패: {e}")

    raise last_error


def _dispatch_llm_call(provider: str, prompt: str, max_tokens: int) -> str:
    """provider별 API 키가 있는지 확인하고 실제 호출. 키 없으면 예외 발생시켜 다음 provider로 넘어가게 함."""
    if provider == "anthropic":
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY 없음")
        return _call_anthropic(prompt, max_tokens)
    elif provider == "openai":
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY 없음")
        return _call_openai(prompt, max_tokens)
    elif provider == "gemini":
        if not GEMINI_API_KEYS:
            raise RuntimeError("GEMINI_API_KEY(S) 없음")
        return _call_gemini(prompt, max_tokens)
    else:
        raise RuntimeError(f"알 수 없는 provider '{provider}'")


_llm_call_timestamps: list = []
LLM_MAX_CALLS_PER_WINDOW = int(os.environ.get("LLM_MAX_CALLS_PER_WINDOW", 8))
LLM_RATE_WINDOW_SECONDS = int(os.environ.get("LLM_RATE_WINDOW_SECONDS", 60))


def _throttle_llm_calls():
    """최근 LLM_RATE_WINDOW_SECONDS(기본 60초) 동안 LLM_MAX_CALLS_PER_WINDOW(기본 8회)를
    넘게 호출하려 하면, 넘지 않을 때까지 대기함. 메시지가 몰려올 때 429(rate limit) 에러가
    나기 전에 미리 속도를 늦춰서 예방하는 용도."""
    global _llm_call_timestamps
    while True:
        now = time.time()
        _llm_call_timestamps = [t for t in _llm_call_timestamps if now - t < LLM_RATE_WINDOW_SECONDS]
        if len(_llm_call_timestamps) < LLM_MAX_CALLS_PER_WINDOW:
            _llm_call_timestamps.append(now)
            return
        wait_time = LLM_RATE_WINDOW_SECONDS - (now - _llm_call_timestamps[0]) + 0.1
        if wait_time > 0:
            log.info(f"LLM 호출 속도 제한 → {wait_time:.1f}초 대기 후 호출")
            time.sleep(wait_time)


def call_llm(prompt: str, max_tokens: int) -> Optional[str]:
    """LLM_PROVIDER로 먼저 시도하고, 실패하면(크레딧 소진 등) LLM_FALLBACK_PROVIDERS에
    설정된 provider들을 순서대로 시도. 전부 실패하면 None 반환.
    속도 제한(_throttle_llm_calls)은 실제 API 요청이 나가는 지점(각 _call_* 함수 내부)에서
    적용됨 — Gemini는 키 하나당 호출도 전부 별도의 실제 요청이라 거기서 개별로 제한해야
    '논리적 호출 1번 = 최대 9번의 진짜 요청'처럼 속도 제한이 무력화되는 걸 막을 수 있음."""

    providers_to_try = [LLM_PROVIDER] + [p for p in LLM_FALLBACK_PROVIDERS if p != LLM_PROVIDER]

    for i, provider in enumerate(providers_to_try):
        try:
            result = _dispatch_llm_call(provider, prompt, max_tokens)
            if i > 0:
                log.info(f"폴백 provider '{provider}'로 호출 성공")
            return result
        except Exception as e:
            # 마지막 provider가 아니면 아직 다른 폴백이 남아있는 정상적인 흐름이라 info로만 기록.
            # 전부 소진됐을 때만 아래에서 error로 남김.
            log.info(f"LLM 호출 실패 (provider={provider}), 다음으로 재시도: {e}")
            continue

    log.error(f"모든 LLM provider 호출 실패 (시도 순서: {providers_to_try})")
    return None


def ask_llm_same_info(text_a: str, text_b: str) -> bool:
    """LLM에게 두 메시지가 같은 정보를 담고 있는지 판단시킴.
    같은 종목/매매방향/가격대를 다루거나, 같은 기사를 링크했더라도 실질적으로
    같은 코멘트/의견을 전달하는 경우만 SAME으로 판단하도록 함."""
    prompt = (
        "아래 두 개의 주식 관련 메시지가 '실질적으로 같은 정보'를 담고 있는지 판단해줘.\n"
        "가장 먼저 확인할 것: 두 메시지의 핵심 주체(회사명, 산업 분야, 인물, 지표명, 자산군 등)가 "
        "완전히 다르면 — 예를 들어 하나는 반도체 제조사(예: Huawei) 뉴스이고 다른 하나는 "
        "제약회사 신약 승인(예: 알츠하이머 치료제) 뉴스처럼 아예 다른 산업/회사를 다룬다면, 또는 "
        "하나는 농축산물 가격 통계(예: 중국 돼지고기 가격)이고 다른 하나는 반도체 파운드리 가격 "
        "인상(예: TSMC) 뉴스처럼 아예 다른 자산군/시장을 다룬다면 — '가격', '상승', '인상' 같은 "
        "흔한 단어가 우연히 겹치더라도, 채널 서명이나 글머리 기호 형식이 비슷해 보여도 절대 "
        "SAME이 될 수 없어. 무조건 DIFFERENT야. 이건 가장 우선하는 원칙이니, 아래 세부 규칙들을 "
        "보기 전에 먼저 이 기준으로 명백히 다른 주체/산업/자산군이면 바로 DIFFERENT로 답해.\n"
        "또한 최우선 원칙: 짧은 캡션(예: '41 -> 37'처럼 지수 변화만 담은 메시지)에 있는 숫자가, "
        "완전히 다른 주제의 메시지 본문 어딘가에 등장하는 숫자(예: '미사일 41발 발사')와 우연히 "
        "일치하는 경우도 있어. 이런 우연한 숫자 일치는 절대 같은 정보의 증거가 아니야 — 두 "
        "메시지가 같은 대상(같은 지수, 같은 자산, 같은 사건)을 가리키는 게 명확할 때만 숫자 "
        "일치를 근거로 삼고, 그냥 우연히 같은 정수가 서로 다른 맥락(공포탐욕지수 값 vs 미사일 "
        "발사 수, 사상자 수 등)에 등장한 것뿐이라면 DIFFERENT로 판단해.\n"
        "또한 최우선 원칙: '매수 사이드카 발동', '매도 사이드카 발동', '정적VI 발동' 같은 "
        "자동 발신 시황 이벤트 알림은 같은 문구가 매일(또는 하루에도 여러 번) 똑같이 반복될 "
        "수 있는 정형화된 알림이야. 한쪽 메시지에 발동 시각이나 등락률 같은 구체적인 식별 "
        "정보가 없고(예: '코스피 매수 사이드카 발동'처럼 짧고 밋밋한 문구뿐), 다른 쪽에는 "
        "구체적인 시각·수치가 있다면(예: '선물 5.17%, 12:41:29'), 같은 문구를 쓴다는 이유만"
        "으로 SAME이라고 단정하면 안 돼 — 식별 정보가 없는 쪽은 어느 날/어느 시점의 발동인지"
        " 텍스트만으로는 알 수 없으므로, 이런 경우는 DIFFERENT로 판단해.\n"
        "같은 정보로 볼 수 있는 경우: 같은 종목, 같은 매매 방향, 비슷한 가격대/목표가를 말하거나, "
        "같은 뉴스 기사를 공유하면서 사실상 같은 취지의 코멘트를 덧붙인 경우.\n"
        "다른 정보로 봐야 하는 경우: 같은 기사/링크를 공유했더라도 서로 다른 의견, 분석, 전망, "
        "또는 추가 정보(예: 자신만의 코멘트, 반박, 다른 종목과의 연관성 등)를 담고 있는 경우.\n"
        "중요: 자동 발신되는 경제지표 메시지처럼 형식(템플릿)이 완전히 똑같아도, 국가/지표명/수치 "
        "등 핵심 데이터가 다르면 반드시 '다른 정보'야. 예를 들어 '중국 CPI 발표'와 '독일 수출 발표'는 "
        "형식이 같아 보여도 완전히 다른 지표에 대한 별개의 정보이므로 DIFFERENT로 판단해야 해. "
        "형식/틀이 아니라 실제 수치와 대상이 같은지를 기준으로 판단해.\n"
        "또한 중요: 같은 사건/수치를 다루더라도, 하나는 '사실 자체에 대한 보도/분석'이고 다른 하나는 "
        "'그 사실에 대한 특정 인물(대통령, CEO 등)의 반응/발언/코멘트'를 전하는 기사라면 서로 다른 "
        "정보로 판단해. 예: 'IMF가 한국 성장률을 2.6%로 상향했다'는 사실 보도와, '대통령이 그 상향 "
        "소식에 소감을 밝혔다'는 반응 보도는 같은 숫자를 언급하지만 담고 있는 뉴스 가치(데이터 자체 "
        "vs 인물의 코멘트)가 다르므로 DIFFERENT야.\n"
        "또한 중요: 핵심 발언/주제가 같더라도, 한쪽 메시지에만 있는 구체적인 추가 정보(예: 다른 "
        "애널리스트/기관의 별도 의견, 특정 종목의 밸류에이션 수치, 구체적인 실적/지표 데이터 등"
        "실행 가능한 추가 데이터포인트)가 있다면 그 추가 정보가 놓치기 아까운 가치가 있는 것으로 "
        "보고 DIFFERENT로 판단해. 단순히 문장을 다르게 표현했거나 순서만 바꾼 경우는 그대로 SAME이야.\n"
        "이 구분을 더 명확히 하자면: 같은 리포트/뉴스를 다루는 두 메시지 중 한쪽이 다른 쪽보다 "
        "짧고 간추려져 있을 때, (a) 짧은 쪽에 없는 내용이 단지 배경설명·인용문·맥락(예: 과거 "
        "사례, 협의 상대, 논평 거부 여부)뿐이라면 실질적으로 같은 정보이므로 SAME이야. 하지만 "
        "(b) 긴 쪽에 짧은 쪽에는 전혀 없는 '구체적인 연도별 수치 전망치'(예: 연도별 영업이익 "
        "전망, PER 등 밸류에이션 배수, 점유율 전망 등 숫자로 된 예측 데이터)가 여러 개 추가로 "
        "담겨 있다면, 이건 투자자가 실제로 판단에 활용할 수 있는 새로운 데이터이므로 DIFFERENT로 "
        "판단해. 예: SK하이닉스 관련 두 메시지가 둘 다 '바클레이즈가 목표주가 330달러로 "
        "커버리지를 개시했다'는 같은 핵심 사실을 다루더라도, 한쪽에만 '2026~2028년 연도별 "
        "영업이익 전망치와 영업이익률', 'PER 8배라는 목표배수', '중국 D램 업체의 캐파/수율 "
        "전망' 같은 구체적 수치 데이터가 담겨 있다면 DIFFERENT야.\n"
        "또한 중요: 하나는 여러 주제를 폭넓게 다루는 종합 브리핑(예: 증시 전반, 여러 섹터/종목을 "
        "짧게짧게 다룸)이고 다른 하나는 그중 한 가지 주제만 훨씬 상세하게 파고든 전문 브리핑(예: "
        "특정 인물의 발언만 여러 섹션에 걸쳐 상세히 정리)이라면, 일부 내용이 겹치더라도 DIFFERENT로 "
        "판단해. 한쪽은 넓고 얕게, 다른 쪽은 좁고 깊게 다루는 건 본질적으로 다른 콘텐츠고, 상세 "
        "브리핑 쪽에만 있는 세부 내용(예: 다른 정치 이슈, 인사, 법적 조치 등)이 종합 브리핑에는 "
        "빠져있다는 게 이 판단의 근거야. 예: '미국 증시 브리핑'에서 호르무즈 해협 관련 소식을 한 "
        "문단 언급했다고 해서, '트럼프 발언 및 정치 정리'라는 훨씬 상세한 전문 브리핑과 SAME으로 "
        "보면 안 돼 — 후자에만 있는 암호화폐 정책, 러시아 제재, 인사, 연설 등의 내용이 사라지게 돼.\n"
        "또한 중요: 같은 채널이 같은 날 '트럼프 발언 정리', '트럼프 발언 진행중'처럼 비슷한 제목으로 "
        "여러 번(계속 업데이트하며) 올리는 시리즈물일 수 있어. 이런 경우 '트럼프', '발언', '이란' "
        "같은 단어가 계속 반복돼서 표면적으로 비슷해 보이지만, 각 메시지가 다루는 📍 섹션 주제 "
        "자체가 서로 다르면(예: 한쪽은 '호르무즈 해협/암호화폐/러시아/인사', 다른 쪽은 '이란 군사 "
        "대응/이스라엘 네타냐후') 같은 인물의 발언을 다룬다는 공통점이 있어도 서로 다른 구체적 "
        "발언/사건을 전달하는 것이므로 DIFFERENT로 판단해. 섹션 제목이 실질적으로 겹치는지를 "
        "기준으로 봐.\n"
        "표현 방식이나 문체 차이는 무시하고 실질적 내용만 봐.\n"
        "답은 반드시 SAME 또는 DIFFERENT 한 단어로만 답해.\n\n"
        f"[메시지 A]\n{text_a}\n\n[메시지 B]\n{text_b}"
    )
    answer = call_llm(prompt, max_tokens=10)
    if answer is None:
        log.warning("LLM 판단 실패, 안전하게 '다른 정보'로 처리")
        return False
    return answer.strip().upper().startswith("SAME")


TRUMP_WORDING_INSTRUCTION = (
    "인물 표기: 트럼프는 2025년 1월 재취임하여 현재 미국의 현직 대통령입니다. "
    "'트럼프 전 대통령'이 아니라 '트럼프 대통령'으로 표기해.\n"
    "한국 대통령은 이재명이 현직입니다. 원문에 '李대통령', '이 대통령'처럼 성씨나 축약형으로만 "
    "나오면 전부 이재명 대통령을 가리키는 것으로 표기해. 성씨가 같다는 이유만으로 이명박, "
    "이승만 등 과거의 다른 인물 이름으로 확장하거나 추측하지 마."
)


def summarize_message(text: str) -> Optional[dict]:
    """전달할 메시지를 요약 + 인사이트로 나눠서 반환.
    반환값: {"summary": str, "insight": Optional[str]} 또는 실패 시 None."""
    prompt = (
        "아래 텔레그램 메시지를 분석해서, 정확히 아래 형식으로만 출력해 (두 줄):\n"
        "[첫 번째 줄] 핵심 사실 요약 1~2문장. 종목명, 매수/매도 방향, 가격/목표가 등 핵심 정보가 "
        "있다면 반드시 포함. 정치/거시경제 뉴스라면 핵심 사건과 시장(주가, 환율, 금리 등)에 미칠 "
        "영향을 중심으로 작성.\n"
        "[두 번째 줄] 이 소식이 관련 종목/섹터/시장에 어떤 영향을 줄 수 있는지 구체적인 인사이트 "
        "1문장 (막연한 감상이 아니라 어떤 산업이나 종목군과 연관되는지 구체적으로). 확실한 연관성이 "
        "없으면 이 줄은 완전히 비워서 출력해(빈 줄).\n"
        "주의: 이 메시지에는 원본에 차트/스크린샷 이미지가 첨부되어 있을 수 있는데 너는 그 이미지를 "
        "볼 수 없어. 텍스트만으로 어떤 종목/자산인지 알 수 없다면 '특정 종목'처럼 부정확하게 "
        "단정하지 말고, 알 수 있는 정보(가격, 등락률, 방향 등)만 사실대로 서술해.\n"
        "라벨(예: '요약:', '인사이트:')이나 번호, 불릿기호는 절대 붙이지 말고, 문장만 줄바꿈으로 "
        "구분해서 딱 두 줄만 출력해. 군더더기 표현(이모지, 홍보 문구, 채널 소개 등)은 빼고 정보만 담아줘.\n\n"
        + TRUMP_WORDING_INSTRUCTION + "\n\n" +
        f"[메시지]\n{text}"
    )
    result = call_llm(prompt, max_tokens=250)
    if not result:
        return None

    lines = [line.strip() for line in result.strip().split("\n")]
    lines = [line for line in lines if line]  # 빈 줄 제거

    if not lines:
        return None
    summary = lines[0]
    insight = lines[1] if len(lines) > 1 else None
    return {"summary": summary, "insight": insight}


def is_regional_briefing(text: str) -> bool:
    """제목이 '글로벌 뉴스 브리핑'인 메시지인지 판단 (그 외 조건 없음)."""
    return "글로벌 뉴스 브리핑" in text


NUMBERED_ITEM_RE = re.compile(r"^\s*\d+\.\s", re.MULTILINE)


POLITICAL_SECTION_RE = re.compile(r"^📍", re.MULTILINE)


SCHEDULE_TIME_PATTERN_RE = re.compile(r"\d{1,2}:\d{2}\s*(?:AM|PM)", re.IGNORECASE)


def is_political_briefing(text: str) -> bool:
    """'📍호르무즈 해협', '📍미국 행정부'처럼 📍 이모지로 구분된 섹션이 3개 이상 있는
    정치/발언 정리 메시지인지 감지.
    다만 '트럼프 대통령 일정'처럼 시간(AM/PM)이 반복되고 📍가 섹션 주제가 아니라 각
    시간대 이벤트의 '장소' 태그로만 쓰이는 일정표는 제외함 (요약+인사이트가 필요 없는
    단순 스케줄이라, 섹션별로 쪼개서 요약하면 오히려 불필요하게 장황해짐)."""
    if len(POLITICAL_SECTION_RE.findall(text)) < 3:
        return False
    if len(SCHEDULE_TIME_PATTERN_RE.findall(text)) >= 3:
        return False  # 시간(AM/PM) 패턴이 반복되면 섹션형 브리핑이 아니라 일정표로 판단
    return True


def summarize_political_briefing(text: str) -> Optional[str]:
    """📍 섹션으로 구성된 정치/발언 정리를, 원문 섹션 제목·이모티콘을 그대로 유지한 채
    섹션마다 요약+인사이트로 정리."""
    prompt = (
        "아래는 📍 이모지로 구분된 여러 섹션으로 구성된 정치/발언 정리 메시지야. "
        "실제 등장하는 섹션마다 아래 형식으로 정리해줘:\n"
        "(원문에 있는 그 섹션의 '📍제목'을 한 글자도 바꾸지 말고 정확히 그대로 복사) : "
        "그 섹션 핵심 내용 1~2문장 요약\n"
        "중요: 섹션 제목과 이모지(📍)는 절대 새로 짓거나 바꾸지 말고 원문에 있는 그대로 정확히 "
        "옮겨 써야 해.\n"
        "그 요약 줄 바로 아래에 공백 4칸을 들여쓴 뒤 '💡 '로 시작하는 그 섹션만의 인사이트 1문장을 "
        "붙여줘 — 이 발언/조치가 시장이나 특정 종목/섹터에 어떤 영향을 줄 수 있는지. 확실한 "
        "연관성이 없으면 생략해도 돼.\n"
        "섹션과 섹션 사이는 빈 줄 1개로 구분해줘.\n"
        "원문에 있는 섹션만 포함하고, 없는 섹션을 새로 만들어내지 마.\n"
        "라벨(예: '요약:')은 붙이지 말고 자연스럽게 적어.\n"
        "마크다운 굵게(**텍스트**)나 마크다운 코드펜스(```)는 절대 쓰지 마.\n\n"
        + TRUMP_WORDING_INSTRUCTION + "\n\n"
        f"[메시지]\n{text}"
    )
    return call_llm(prompt, max_tokens=6000) or None


MARKET_INDEX_LINE_RE = re.compile(r"^[📈📉]\s", re.MULTILINE)


def is_market_close_snapshot(text: str) -> bool:
    """'미장 마감'처럼 지수/자산 가격(📈/📉 + 이름 + 숫자)만 나열하는 스냅샷 메시지인지 감지.
    이미 숫자 데이터라 요약할 게 없고, 오히려 LLM 요약을 거치면 숫자가 왜곡되거나 누락될
    위험이 있어서 원문을 그대로 전달하는 데 사용됨."""
    return "미장 마감" in text and len(MARKET_INDEX_LINE_RE.findall(text)) >= 3


def is_reddit_analysis(text: str) -> bool:
    """'미국 레딧 게시물 분석'처럼 종목/주제별 섹션(이모지+제목, '·' 불릿)으로 구성된
    레딧 여론 요약인지 감지."""
    return "레딧 게시물 분석" in text


def summarize_reddit_analysis(text: str) -> Optional[str]:
    """종목/주제별 섹션으로 구성된 레딧 게시물 분석을, 섹션마다 요약+인사이트로 정리."""
    prompt = (
        "아래는 종목/주제별 섹션(이모지+종목명 또는 주제명, '·'로 시작하는 불릿 목록)으로 "
        "구성된 레딧(WSB 등) 여론 분석이야. 실제 등장하는 섹션마다 아래 형식으로 정리해줘:\n"
        "섹션명(종목/주제명): 그 섹션 여론의 핵심 1~2문장 요약\n"
        "그 바로 아래 줄에 공백 4칸을 들여쓴 뒤 '💡 '로 시작하는 그 섹션만의 인사이트 1문장을 "
        "붙여줘 — 이 여론이 실제 주가/투자 판단에 어떤 시사점이 있는지. 확실한 연관성이 없으면 "
        "생략해도 돼.\n"
        "섹션과 섹션 사이는 빈 줄 1개로 구분해줘.\n"
        "실제 원문에 있는 섹션만 포함하고, 없는 섹션을 새로 만들어내지 마.\n"
        "라벨(예: '요약:')은 붙이지 말고, 섹션명과 내용만 자연스럽게 적어.\n"
        "마크다운 굵게(**텍스트**)나 마크다운 코드펜스(```)는 절대 쓰지 마.\n\n"
        + TRUMP_WORDING_INSTRUCTION + "\n\n"
        f"[메시지]\n{text}"
    )
    return call_llm(prompt, max_tokens=2500) or None


def is_market_close_briefing(text: str) -> bool:
    """'글로벌 마감 시황 브리핑'처럼 여러 섹션(섹터별 동향, 특징주, 시장 분석 등)과
    '한 줄 결론'으로 구성된 정기 시황 브리핑인지 감지."""
    return "시황 브리핑" in text and "한 줄 결론" in text


def summarize_market_close_briefing(text: str) -> Optional[str]:
    """섹션별 구성 + '한 줄 결론'으로 끝나는 시황 브리핑을 처리.
    각 섹션은 요약+인사이트로, '한 줄 결론'은 요약하지 않고 원문 그대로(인사이트 없이) 전달."""
    prompt = (
        "아래는 여러 섹션(예: 미국 증시 개요, 섹터별 동향, 특징주, 주요 경제 지표, 시장 분석, "
        "투자 유의 사항 등)과 마지막에 '한 줄 결론'으로 구성된 시황 브리핑이야.\n"
        "1) '한 줄 결론'을 제외한 나머지 각 섹션마다 아래 형식으로 정리해줘:\n"
        "   섹션명: 핵심 내용 1~2문장 요약\n"
        "   그 바로 아래 줄에 공백 4칸을 들여쓴 뒤 '💡 '로 시작하는 그 섹션만의 인사이트 1문장을 "
        "붙여줘 (확실한 연관성이 없으면 생략 가능).\n"
        "   섹션과 섹션 사이는 빈 줄 1개로 구분해줘.\n"
        "   실제 원문에 있는 섹션만 포함하고, 없는 섹션을 새로 만들어내지 마.\n"
        "2) '한 줄 결론'은 절대 요약하거나 문장을 바꾸지 말고, 원문 문장을 정확히 그대로 옮겨줘. "
        "이 섹션에는 인사이트(💡)를 붙이지 마. 앞에 빈 줄 1개로 구분하고, '한 줄 결론: '이라는 "
        "표현은 그대로 유지해줘.\n"
        "라벨(예: '요약:')은 붙이지 말고, 섹션명과 내용만 자연스럽게 적어.\n"
        "마크다운 굵게(**텍스트**)나 마크다운 코드펜스(```)는 절대 쓰지 마.\n\n"
        + TRUMP_WORDING_INSTRUCTION + "\n\n"
        f"[메시지]\n{text}"
    )
    return call_llm(prompt, max_tokens=3800) or None


QNA_COLON_LINE_RE = re.compile(r"^\s*:\s", re.MULTILINE)


def is_qna_briefing(text: str) -> bool:
    """'에릭슨 2Q26 Q&A'처럼 '주제\\n: 답변' 형식으로 여러 질의응답이 나열된 실적 Q&A
    요약인지 감지. 제목에 'Q&A'가 있고, ':'로 시작하는 답변 줄이 3개 이상이면 해당 형식으로 간주."""
    return "Q&A" in text and len(QNA_COLON_LINE_RE.findall(text)) >= 3


def summarize_qna_briefing(text: str) -> Optional[str]:
    """'주제\\n: 답변' 형식의 실적 Q&A를, 원문 주제 제목을 그대로 유지한 채
    주제마다 요약+인사이트로 정리."""
    prompt = (
        "아래는 '주제' 줄 다음에 ': 답변' 줄이 이어지는 형식으로 구성된 실적 Q&A 요약이야. "
        "실제 등장하는 주제마다 아래 형식으로 정리해줘:\n"
        "(원문에 있는 그 주제의 제목을 한 글자도 바꾸지 말고 정확히 그대로 복사): 답변 핵심을 "
        "1~2문장으로 요약\n"
        "중요: 주제 제목은 절대 새로 짓거나 바꾸지 말고 원문에 있는 그대로 정확히 옮겨 써야 해.\n"
        "그 요약 줄 바로 아래에 공백 4칸을 들여쓴 뒤 '💡 '로 시작하는 그 주제만의 인사이트 1문장을 "
        "붙여줘 — 이 답변이 그 회사의 실적/전략/주가에 어떤 의미가 있는지 구체적으로. 확실한 "
        "연관성이 없으면 생략해도 돼.\n"
        "주제와 주제 사이는 빈 줄 1개로 구분해줘.\n"
        "원문에 있는 주제만 포함하고, 없는 주제를 새로 만들어내지 마.\n"
        "라벨(예: '요약:')은 붙이지 말고 자연스럽게 적어.\n"
        "마크다운 굵게(**텍스트**)나 마크다운 코드펜스(```)는 절대 쓰지 마.\n\n"
        + TRUMP_WORDING_INSTRUCTION + "\n\n"
        f"[메시지]\n{text}"
    )
    return call_llm(prompt, max_tokens=2500) or None


def is_numbered_news_list(text: str) -> bool:
    """'1. 헤드라인 ... 2. 헤드라인 ...'처럼 번호 매겨진 여러 뉴스 항목이 한 메시지에
    같이 오는 섹터 다이제스트 형태인지 감지. 번호 항목이 3개 이상이면 해당 형식으로 간주.
    이런 메시지는 항목별로 따로 요약해야 서로 다른 회사/이슈 내용이 뭉개지지 않음."""
    return len(NUMBERED_ITEM_RE.findall(text)) >= 3


def summarize_numbered_news_list(text: str) -> Optional[str]:
    """번호 매겨진 여러 뉴스 항목(섹터 다이제스트 등)을 요약.
    원문에서 빈 줄 없이 연달아 묶인 번호들은 그 묶음 전체를 하나의 주제로 통합 요약하고,
    앞뒤로 빈 줄에 둘러싸여 홀로 있는 번호는 그 번호 하나만으로 개별 요약함."""
    prompt = (
        "아래는 번호가 매겨진 여러 뉴스 헤드라인이 나열된 다이제스트야. 원문을 보면 서로 관련된 "
        "번호들이 빈 줄 없이 연달아 묶여 있다가, 다음 묶음과는 빈 줄로 구분되는 구조로 되어 있어. "
        "이 구조를 그대로 활용해서 요약해줘:\n"
        "1) 빈 줄 없이 연달아 묶인 번호들은 하나의 묶음으로 보고, 그 묶음 전체를 관통하는 주제로 "
        "통합해서 요약+인사이트를 딱 1세트만 만들어줘. 묶음 안의 번호들을 각각 따로 요약하지 마.\n"
        "   형식: 포함된 번호 범위(예: '2-4번') 또는 해당 번호들: 그 묶음의 핵심 내용을 1~2문장으로 "
        "통합 요약\n"
        "   그 바로 아래 줄에 공백 4칸을 들여쓴 뒤 '💡 '로 시작하는 그 묶음만의 인사이트 1문장을 "
        "붙여줘 (확실한 연관성이 없으면 생략 가능).\n"
        "2) 앞뒤로 빈 줄에 둘러싸여 번호 하나만 홀로 있는 경우는, 그 번호 하나만으로 같은 형식 "
        "('번호: 요약' + 인사이트)을 만들어줘.\n"
        "묶음과 묶음 사이는 빈 줄 1개로 구분해줘.\n"
        "라벨(예: '요약:')은 붙이지 말고, 번호(범위)와 요약 내용만 자연스럽게 적어.\n"
        "마크다운 굵게(**텍스트**)나 마크다운 코드펜스(```)는 절대 쓰지 마.\n\n"
        + TRUMP_WORDING_INSTRUCTION + "\n\n"
        f"[메시지]\n{text}"
    )
    return call_llm(prompt, max_tokens=2800) or None


def summarize_regional_briefing(text: str) -> Optional[str]:
    """지역별로 구획된 뉴스 브리핑을 지역마다 1~2문장으로 나눠 요약."""
    prompt = (
        "아래는 지역별로 구획된 글로벌 뉴스 브리핑이야. 각 지역 섹션의 핵심 내용을 "
        "1~2문장으로 요약해서, 지역별로 줄바꿈해서 정리해줘.\n"
        "형식 예시(실제 등장하는 지역만 포함하고, 순서는 원문 순서를 따라):\n"
        "🇺🇸 북미: (요약)\n"
        "🌎 남미: (요약)\n"
        "🇪🇺 유럽: (요약)\n"
        "🌍 중동: (요약)\n"
        "🌏 아시아: (요약)\n"
        "🌍 아프리카: (요약)\n"
        "🌏 오세아니아: (요약)\n"
        "규칙:\n"
        "- 원문에 없는 지역은 아예 줄을 만들지 마.\n"
        "- '특이사항 없음'처럼 내용이 없는 지역도 줄을 만들지 말고 생략해.\n"
        "- 각 지역 요약은 그 지역에서 가장 중요한 사건 위주로, 최대 2문장.\n"
        "- 주가/환율/금리 등 시장에 영향을 줄 수 있는 내용이면 그 부분을 우선적으로 반영해.\n"
        "- 위 형식 외에 다른 말(제목, 인사말, 출처 등)은 절대 덧붙이지 마.\n\n"
        + TRUMP_WORDING_INSTRUCTION + "\n\n"
        f"[메시지]\n{text}"
    )
    return call_llm(prompt, max_tokens=600) or None


def has_real_photo(message) -> bool:
    """이 메시지에 '진짜' 첨부 사진이 있는지 확인. Telethon의 message.photo 속성은
    실제 첨부 사진뿐 아니라 링크 미리보기(웹페이지 프리뷰)에 딸린 썸네일 이미지도
    같이 반환해서, X(트위터) 링크 등을 공유한 메시지의 미리보기 썸네일까지 '진짜 첨부
    사진'으로 오인해 전송하는 오탐의 원인이었음. media 타입이 정확히 MessageMediaPhoto인
    경우만 진짜 첨부 사진으로 인정함."""
    return isinstance(message.media, MessageMediaPhoto)


def is_html_document(message) -> bool:
    """메시지에 첨부된 파일이 HTML 문서인지 확인 (뉴스 다이제스트 대시보드 등)."""
    doc = getattr(message, "document", None)
    if not doc:
        return False
    mime = (getattr(doc, "mime_type", "") or "").lower()
    filename = (getattr(getattr(message, "file", None), "name", "") or "").lower()
    return "html" in mime or filename.endswith(".html") or filename.endswith(".htm")


def extract_webpage_preview(message) -> Optional[dict]:
    """메시지에 링크만 달랑 있을 때 텔레그램이 자동 생성하는 미리보기 카드(제목/설명/사이트명)를
    추출. 이게 없으면 LLM은 URL 문자열 하나만 보고 요약해야 해서 부실한 답이 나옴."""
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaWebPage):
        webpage = getattr(media, "webpage", None)
        if isinstance(webpage, WebPage):
            return {
                "site_name": getattr(webpage, "site_name", None),
                "title": getattr(webpage, "title", None),
                "description": getattr(webpage, "description", None),
            }
    return None


def html_to_text(html_content: str) -> str:
    """HTML에서 <script>/<style> 태그를 통째로 제거하고, 나머지 태그도 걷어내서
    사람이 읽는 순수 텍스트만 남김 (뉴스 제목, 카테고리명 등)."""
    text = re.sub(r"<script.*?</script>", " ", html_content, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def summarize_html_digest(text: str) -> Optional[str]:
    """카테고리별로 구성된 뉴스 다이제스트(HTML에서 추출한 텍스트)를,
    카테고리마다 요약 + 그 카테고리만의 인사이트를 붙여서 정리."""
    prompt = (
        "아래는 카테고리별로 구성된 뉴스 다이제스트에서 텍스트만 추출한 내용이야 "
        "(예: 국내/금융, 원자재, 테크, 모빌리티, 미디어 등으로 구분된 뉴스 헤드라인 모음).\n"
        "실제 등장하는 카테고리마다 아래 형식으로 정리해줘:\n"
        "카테고리명: 그 카테고리 핵심 뉴스 1~2문장 요약\n"
        "💡 그 카테고리 뉴스가 관련 종목/섹터에 어떤 영향을 줄 수 있는지 구체적인 인사이트 1문장\n"
        "(카테고리마다 이 두 줄을 반복, 카테고리 사이는 빈 줄로 구분)\n"
        "규칙:\n"
        "- 뉴스가 없거나 카테고리 이름을 알 수 없으면 그 카테고리는 통째로 생략해.\n"
        "- 인사이트는 확실한 연관성이 있을 때만 작성하고, 억지로 만들어내지 말고 애매하면 "
        "💡 줄 자체를 생략해도 돼.\n"
        "- 라벨(예: '요약:', '인사이트:')은 붙이지 말고 카테고리명만 그대로 써.\n"
        "- 위 형식 외에 다른 말(전체 총평, 인사말, 출처 등)은 절대 덧붙이지 마.\n\n"
        + TRUMP_WORDING_INSTRUCTION + "\n\n"
        f"[뉴스 다이제스트 내용]\n{text[:10000]}"
    )
    return call_llm(prompt, max_tokens=800) or None


def _kst_bounds_today(start_hour: int, end_hour: int) -> tuple:
    """한국시간 기준 start_hour~end_hour 구간을 UTC datetime 쌍으로 반환.
    start_hour >= end_hour면(예: 18시~다음날 8시처럼 자정을 걸치는 야간 구간) 시작을
    전날로 계산함."""
    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)  # 값만 KST로 밀어둔 것 (tzinfo는 그대로 UTC)
    end_kst = now_kst.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if start_hour >= end_hour:
        start_kst = (end_kst - timedelta(days=1)).replace(hour=start_hour, minute=0, second=0, microsecond=0)
    else:
        start_kst = end_kst.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    start_utc = start_kst - timedelta(hours=9)
    end_utc = end_kst - timedelta(hours=9)
    return start_utc, end_utc


MARKET_QUANT_TICKERS = {
    "S&P 500": "^GSPC",
    "나스닥 100": "^NDX",
    "러셀 2000": "^RUT",
    "VIX 공포지수": "^VIX",
    "필라델피아 반도체지수": "^SOX",
    "달러인덱스(DXY)": "DX-Y.NYB",
    "원/달러": "KRW=X",
    "Brent유": "BZ=F",
    "WTI유": "CL=F",
    "Henry Hub 천연가스": "NG=F",
    "유럽 TTF 천연가스": "TTF=F",
    # 아래 둘은 최종 텍스트에 그대로 노출하지 않고, 3-2-1 정제마진(크랙 스프레드) 계산에만
    # 씀(compute_crack_spread_321 참고) — format_quant_data_text에서 필터링됨
    "RBOB 가솔린(크랙 스프레드 계산용)": "RB=F",
    "난방유(크랙 스프레드 계산용)": "HO=F",
}

# 오후(18시) 정리에서만 추가로 조회하는 아시아 지수 — 오전엔 아직 장이 열리지 않았거나
# 막 열린 시점이라 의미가 없어서 제외함
ASIA_QUANT_TICKERS = {
    "코스피": "^KS11",
    "코스닥": "^KQ11",
    "니케이225": "^N225",
    "항셍지수": "^HSI",
    "상해종합지수": "000001.SS",
}

# FRED(세인트루이스 연준)에서 API 키 없이 받을 수 있는 공개 CSV로 조회하는 금리 시계열.
# yfinance에는 10년 실질금리(TIPS)나 기대인플레이션(BEI) 지수가 없어서 FRED로 별도 조회함.
# 값 단위는 전부 %이므로 전일 대비 변화는 %p가 아니라 bp(basis point)로 표기.
FRED_RATE_SERIES = {
    "미국 10년물 명목금리": "DGS10",
    "미국 2년물 명목금리": "DGS2",
    "미국 10년 TIPS 실질금리": "DFII10",
    "미국 10년 기대인플레이션(BEI)": "T10YIE",
}


def _fetch_yfinance_quant(tickers: dict) -> Optional[dict]:
    """yfinance로 tickers({지표명: 티커}) 각각의 최신 종가/전일 종가/등락률을 조회해서
    {지표명: {"value", "prev_value", "change_pct"}} 형태로 반환. 개별 티커 조회 실패는
    건너뛰고 계속 진행하며, 전부 실패하면 None을 반환해서(다른 정량 데이터 조회 함수들과
    동일하게) 정리 기능 전체가 죽지 않도록 함."""
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance 라이브러리가 설치되어 있지 않아 정량 데이터 조회를 건너뜀 "
                     "(pip install yfinance 필요)")
        return None

    result = {}
    for name, ticker in tickers.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if len(hist) < 2:
                log.info(f"{name}({ticker}) 데이터 부족, 건너뜀")
                continue
            latest = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            change_pct = (latest - prev) / prev * 100 if prev else 0.0
            result[name] = {"value": latest, "prev_value": prev, "change_pct": change_pct}
        except Exception as e:
            log.info(f"{name}({ticker}) 시세 조회 실패, 건너뜀: {e}")
            continue

    if not result:
        log.warning("정량 시장 데이터를 하나도 못 가져옴 (네트워크 또는 API 문제로 추정)")
        return None
    return result


def fetch_market_quant_data() -> Optional[dict]:
    """주식/변동성/환율/에너지 정량 데이터(MARKET_QUANT_TICKERS) 조회."""
    return _fetch_yfinance_quant(MARKET_QUANT_TICKERS)


def fetch_asia_quant_data() -> Optional[dict]:
    """오후 정리 전용 아시아 지수(ASIA_QUANT_TICKERS) 조회."""
    return _fetch_yfinance_quant(ASIA_QUANT_TICKERS)


def fetch_fred_series_latest(series_id: str) -> Optional[dict]:
    """FRED의 fredgraph.csv 공개 엔드포인트(API 키 불필요)에서 시계열의 최신 값과 그 직전
    값을 조회. FRED는 휴장일 등 값이 없는 날을 '.'으로 표기하므로 걸러내고 실제 값이 있는
    마지막 두 지점을 비교함. 실패해도 항상 None을 반환해서 정리 기능 전체가 죽지 않도록 함."""
    try:
        resp = requests.get(
            f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", timeout=10,
        )
        resp.raise_for_status()
        rows = [r.split(",") for r in resp.text.strip().split("\n")[1:] if r.strip()]
        valid = [(d, float(v)) for d, v in rows if v not in (".", "")]
        if len(valid) < 2:
            return None
        (prev_date, prev_value), (latest_date, latest_value) = valid[-2], valid[-1]
        return {
            "value": latest_value, "date": latest_date,
            "prev_value": prev_value, "prev_date": prev_date,
            "change_bp": (latest_value - prev_value) * 100,
        }
    except Exception as e:
        log.info(f"FRED 시계열({series_id}) 조회 실패, 건너뜀: {e}")
        return None


def fetch_rates_quant_data() -> Optional[dict]:
    """FRED_RATE_SERIES에 정의된 금리 시계열(명목/실질/BEI)을 전부 조회."""
    result = {}
    for name, series_id in FRED_RATE_SERIES.items():
        data = fetch_fred_series_latest(series_id)
        if data:
            result[name] = data
    return result or None


def compute_crack_spread_321(quant_data: dict) -> Optional[dict]:
    """WTI/RBOB/난방유 선물가로 미국 3-2-1 크랙 스프레드($/bbl)를 계산. RBOB·난방유는
    $/gal 단위라 배럴당 환산(×42)이 필요함. 셋 중 하나라도 조회 실패했으면 None."""
    wti = quant_data.get("WTI유")
    rbob = quant_data.get("RBOB 가솔린(크랙 스프레드 계산용)")
    heat = quant_data.get("난방유(크랙 스프레드 계산용)")
    if not (wti and rbob and heat):
        return None

    def _crack(wti_v, rbob_v, heat_v):
        return (2 * rbob_v * 42 + 1 * heat_v * 42 - 3 * wti_v) / 3

    value = _crack(wti["value"], rbob["value"], heat["value"])
    prev_value = _crack(wti["prev_value"], rbob["prev_value"], heat["prev_value"])
    return {"value": value, "prev_value": prev_value, "change": value - prev_value}


_CRACK_SPREAD_INPUT_NAMES = ("RBOB 가솔린(크랙 스프레드 계산용)", "난방유(크랙 스프레드 계산용)")


def format_quant_data_text(quant_data: dict) -> str:
    """fetch_market_quant_data()/fetch_asia_quant_data() 결과를 LLM 프롬프트용 텍스트로 정리.
    크랙 스프레드 계산 전용 원자재(RBOB/난방유)는 최종 텍스트에서 제외(계산된 크랙 스프레드
    값만 별도로 노출됨)."""
    lines = [
        f"- {name}: {v['value']:,.2f} (전일 대비 {v['change_pct']:+.2f}%)"
        for name, v in quant_data.items() if name not in _CRACK_SPREAD_INPUT_NAMES
    ]
    crack = compute_crack_spread_321(quant_data)
    if crack:
        lines.append(f"- 미국 3-2-1 크랙 스프레드(정제마진): ${crack['value']:.2f}/bbl (전일 대비 {crack['change']:+.2f})")
    return "\n".join(lines)


def format_rates_data_text(rates_data: dict) -> str:
    """fetch_rates_quant_data() 결과를 LLM 프롬프트용 텍스트로 정리. bp 단위로 표기."""
    return "\n".join(
        f"- {name}: {v['value']:.2f}% (기준일 {v['date']}, 전일 대비 {v['change_bp']:+.0f}bp)"
        for name, v in rates_data.items()
    )


DAILY_DIGEST_ROLE_INSTRUCTION = (
    "너는 글로벌 매크로·주식·원자재를 함께 보는 기관투자자용 Daily Market Strategist야. 이 "
    "브리핑의 목적은 '2027년 에너지가 오를 것이라는 주장을 매일 강화하는 것'이 아니라, 2027년 "
    "에너지 강세 가설이 실제 원유·가스·전력·재고·금리·기업 데이터에 의해 강화되고 있는지 또는 "
    "약화되고 있는지를 매일 객관적으로 검증하는 것이야. 단순 뉴스 요약이 아니라 ①금리(명목/실질/"
    "기대인플레이션) ②에너지(원유/정제마진/천연가스/LNG/전력) ③AI·데이터센터 전력수요 ④지정학 "
    "⑤인플레이션(헤드라인 및 2차 전이) ⑥글로벌 자금흐름 및 자산 간 로테이션을 하나의 연결된 "
    "프레임으로 해석해.\n\n"
    "[핵심 분석 프레임]\n"
    "A. 금리축: 경기/고용/물가 → Fed 및 글로벌 중앙은행 → 미국 국채금리 → 달러 → 성장주/가치주/"
    "중소형주 → 글로벌 증시. 미국 10년 명목금리는 10년 TIPS 실질금리와 10년 BEI(기대인플레이션)"
    "로 근사 분해해서, 오늘 10년물 금리 변화가 BEI 주도인지 실질금리 주도인지를 반드시 구분해줘. "
    "단, BEI는 순수한 기대인플레이션 자체가 아니라 인플레이션 위험프리미엄·유동성 등에도 영향받을"
    " 수 있다는 점을 감안해서 해석해.\n"
    "B. 에너지축: 원유 → 정제마진 → 천연가스/LNG → 전력 → 생산자물가/소비자물가 → 기대인플레이션"
    "(BEI) → 장기금리 → 기업 원가·마진 → 산업별 실적·주가. 에너지 가격 변화를 절대 '유가가 올랐다"
    "/내렸다'로 끝내지 말고 이 연쇄 전체로 해석해. 예시 패턴: 유가↑+10Y↑(BEI주도)→인플레 우려·"
    "가치주 상대강세 점검, 유가↑+10Y↑(실질금리주도)→긴축우려·성장주 밸류에이션 압박 점검, 유가↑"
    "+10Y↓→인플레보다 리스크오프/경기둔화 우려 점검, 유가↑+정제마진↓→원재료만 상승(수요 부진), "
    "유가↑+정제마진↑→공급제약+수요 동시 타이트, TTF↑+유럽주↓+EUR↓→유럽 에너지쇼크 점검, AI "
    "CAPEX↑+전력·가스·원전주↑→AI투자가 반도체를 넘어 에너지 인프라로 확산되는지 확인. 금리축과 "
    "에너지축이 실제로 서로 연결되는지 매일 확인하되, 연결이 뚜렷하지 않으면 그렇다고 사실대로 "
    "써(억지로 인과관계를 만들지 마).\n\n"
    "[에너지 관찰 지표]\n"
    "Brent, WTI, Henry Hub, 유럽 TTF, JKM LNG(의미 있는 변화가 있을 때만), 미국 원유·천연가스 "
    "재고, EU 가스 저장률, 정제마진(가능하면 미국 3-2-1 크랙 스프레드로 통일 — 다른 기준을 쓰면 "
    "반드시 명시), OPEC+ 생산 및 여유생산능력, 미국 원유·가스 생산, 중동·러시아 공급 차질, LNG "
    "수출입 및 주요 수출시설 가동, 해상운임/VLCC(공급망 이슈 발생 시), 미국 전력수요/발전원 변화"
    "(AI·데이터센터 이슈 발생 시). 숫자를 무조건 나열하지 말고, 전일 대비 또는 최근 추세 대비 "
    "'시장 해석을 바꿀 만큼 의미 있게 움직인 변수'만 본문에 포함해.\n\n"
    "[2027 에너지 구조적 관찰 프레임 — 확정된 전망이 아니라 매일 검증/반박할 투자 가설]\n"
    "1) 원유: 과거 CapEx 부족→신규 공급 제한 가능성 + 낮은 재고 + 지정학 리스크 → 공급부족 "
    "가능성. 검증 지표: 미국/OECD 원유재고, OPEC+ 생산·spare capacity, 미국 생산, Rig Count, "
    "E&P CapEx, Brent 선물곡선, 정제마진.\n"
    "2) 천연가스: 유전투자 부족→저비용 수반가스 공급 제한 가능성 + 미국 LNG 수출 증가·발전용 "
    "가스수요 증가·글로벌 LNG 수요 → Henry Hub 타이트닝 가능성. 검증 지표: Henry Hub, 미국 "
    "재고·생산량, LNG Feedgas·수출능력, 발전용 가스수요, 날씨/HDD·CDD.\n"
    "3) 유럽: 가뭄/강수부족→수력발전 감소, 하천 수위·수온 문제→원전 냉각 제약→원전 발전량 감소"
    " 가능성→가스발전 수요 증가→TTF 상승 가능성. 날씨+수력+원전+LNG수입+가스저장률을 함께 봐.\n"
    "4) AI/데이터센터: AI CAPEX 증가→데이터센터 증설→전력수요 증가→천연가스 발전/원전/재생에너지"
    "/가스터빈/전력망/변압기 수요 증가 가능성. 'AI→반도체'에서 끝내지 말고 'AI→데이터센터→전력→"
    "천연가스/원전/전력망'까지 반드시 연결해.\n"
    "5) 2차 인플레이션: 천연가스↑→암모니아/질소비료 원가↑, 원유·가스↑→황/인산계 비료 원가↑, "
    "비료↑→곡물 생산비↑→식품 인플레이션. 이 연결고리가 실제 데이터(천연가스/비료/밀/옥수수/대두/"
    "해상운임)에 나타나는지 확인.\n\n"
    "[중요: 2027 에너지 가설 반증 원칙]\n"
    "가설을 지지하는 뉴스·데이터만 선택적으로 쓰지 마. 가설을 약화시키는 데이터(미국 원유·가스 "
    "생산 예상보다 빠른 증가, OPEC+ 증산 및 spare capacity 회복, 미국/OECD 원유재고 증가, 미국 "
    "천연가스 재고 증가, LNG 신규 공급 예상보다 빠른 확대, 유럽 가스저장률 상승, 온화한 겨울, "
    "산업용 에너지 수요 둔화, 중국·글로벌 경기둔화, 정제마진 급락, 선물곡선 Contango 전환, 에너지"
    " 기업 CapEx 증가)도 동일한 비중으로 확인하고, 나타나면 '2027 에너지 강세 가설을 약화시키는 "
    "신호'라고 명확히 써. 반대되는 데이터를 억지로 강세 논리로 해석하지 마.\n\n"
)

DAILY_DIGEST_WRITING_PRINCIPLES = (
    "[작성 원칙]\n"
    "1) 분량: 전체 공백 포함 약 1,200~1,800자를 목표로 해. 조건부 섹션은 오늘 이슈가 없으면 "
    "과감히 생략해. 분량 초과 시 삭제 우선순위: 핵심 이슈 → 시장 온도계 → Energy Pulse → 금리×"
    "에너지 연결 → 한국장/미장 체크포인트 순으로 보존하고, 조건부 섹션·부차 설명부터 삭제해. "
    "핵심 이슈는 최대 4개, Energy Pulse에서 다루는 핵심 이슈는 최대 3개로 제한해.\n"
    "2) 톤: 기관투자자 데스크 노트처럼 군더더기 없이 간결하게 써. '상당히/굉장히/엄청난' 같은 "
    "과도한 수식어는 쓰지 마.\n"
    "3) 기준시점: 가격·지표는 기준 시점과 전일 대비 변화율 또는 bp를 명시해. 종가 데이터와 "
    "실시간 데이터를 혼용하면 반드시 구분해줘(예: 미국장 종가 기준 / 08:10 KST 현재 / 최신 주간"
    " EIA 기준). 원유재고·가스재고·EU 가스저장률처럼 업데이트 주기가 느린 데이터를 당일 실시간"
    "처럼 표현하지 마.\n"
    "4) 사실과 가설 분리: '유럽 전력이 부족하다'처럼 단정하지 말고 '가뭄으로 수력·원전 발전이 "
    "제약될 경우 가스발전 의존도가 높아질 가능성이 있다'처럼 조건부로 써. 2027 에너지 강세는 "
    "확정된 전망이 아니라 계속 검증할 투자 가설로 취급해.\n"
    "5) 출처: 아래 [오늘의 정보 모음]의 텔레그램/리서치 자료를 우선 참고하되, 그 자료의 '주장'과"
    " '현재 실제 시장 데이터'(아래 정량 데이터)를 구분해. 특정 증권사·애널리스트 전망을 시장 "
    "컨센서스처럼 표현하지 마. 출처가 불분명한 수치는 쓰지 마.\n"
    "6) 원유와 가스 분리: Oil은 재고/OPEC+/미국생산/중동/러시아/정제마진/선물곡선, Gas는 날씨/"
    "재고/LNG/발전수요/유럽 수력·원전/LNG 시설가동률로 각각 분석하고, 가격 변화가 공급쇼크인지 "
    "수요변화인지 날씨인지 달러·금리 등 금융요인인지 구분해.\n"
    "7) 자금흐름과 가격 로테이션 구분: ETF Flow·포지셔닝·외국인/기관 수급 등 실제 Flow 데이터가"
    " 없으면 '자금이 유입/이탈됐다'고 단정하지 말고 '상대적 강세', '로테이션 양상', '선호 이동 "
    "가능성' 등으로 표현해.\n"
    "8) 금리 분해: 10년물 변화는 BEI 주도인지 실질금리 주도인지 먼저 확인하고, BEI를 순수 "
    "기대인플레이션과 완전히 동일시하지 마.\n"
    "9) 반증 원칙: 2027 에너지 강세 가설에 불리한 데이터도 같은 비중으로 다루고, 가설이 약해지고"
    " 있으면 명확히 그렇게 써.\n"
    "10) 과해석 금지: 상관관계를 항상 인과관계로 단정하지 말고, 근거가 부족하면 '뚜렷한 연결은 "
    "아직 확인되지 않는다'라고 써.\n"
    "11) 방향성이 모호하면 억지로 Bullish/Bearish 관점을 만들지 말고 '방향성 탐색 구간', '혼재된"
    " 신호', '확인 필요' 등으로 표현해.\n"
    "12) 서식: 이모지는 지정된 섹션 제목에서만 사용하고, 불필요한 이모지나 감탄 표현은 쓰지 마. "
    "각 섹션에서 실제로 다룰 이슈/조건이 없으면 그 섹션은(필수 섹션이 아닌 한) 제목째 통째로 "
    "생략해.\n\n"
)

DAILY_DIGEST_FORMAT_GUARD = (
    "라벨(예: '핵심 이슈:', '인사이트:')은 붙이지 말고 자연스러운 글로 작성해(단, 이슈별 인사이트"
    " 줄 앞의 '💡 시장 영향: '과 지정된 섹션 제목은 표기 그대로 유지). 섹션 제목(이모지 포함)은 "
    "지정된 표기를 정확히 그대로 사용하고, 섹션 사이는 빈 줄 1개로 구분해줘.\n"
    "중요: 마크다운 굵게 표시(**텍스트**)나 마크다운 코드펜스(```)를 절대 쓰지 마. 이 결과는 "
    "우리 쪽에서 이미 <blockquote> 태그로 감싸서 인용블럭 형태로 전송하기 때문에, 네가 마크다운 "
    "기호나 코드펜스를 또 쓰면 별표(**)나 백틱(```)이 그대로 글자로 노출돼서 지저분해 보여. "
    "순수 텍스트로만, 백틱이나 별표 없이 작성해.\n"
    "중복되거나 사소한 내용은 과감히 생략하고, 정말 중요한 것 위주로 압축해.\n\n"
)

DAILY_DIGEST_MORNING_PART1 = (
    "[PART 1 : 밤사이 핵심 이슈]\n"
    "한국장 개장 전 기준이야. 전일 미국장 마감과 밤사이 매크로·에너지 변화를 바탕으로, 오늘 "
    "한국장에 영향을 줄 핵심 이슈 3~4개를 골라줘. 각 이슈마다 아래 형식으로:\n"
    "   - 핵심 뉴스 요약 1~2문장 (글머리 기호 '-' 사용)\n"
    "   그 바로 아래 줄에 공백 4칸(스페이스 4개)으로 들여쓴 뒤 '💡 시장 영향: '으로 시작하는 "
    "문장 1개 — 단순 뉴스 설명이 아니라 왜 금융시장에 중요한지. 가능하면 '지정학→에너지→"
    "인플레이션→BEI/실질금리→주식 밸류에이션' 또는 'AI→데이터센터→전력→천연가스/원전/전력망' "
    "연결고리를 활용하되, 중요도가 낮거나 연결이 약하면 억지로 에너지와 연결하지 마. 확실한 "
    "연관성이 없으면 인사이트 줄을 생략해도 돼.\n"
    "   이슈 묶음 사이는 빈 줄 1개로 구분해줘.\n"
    "PART 1 제목 줄은 정확히 'PART 1: 밤사이 핵심 이슈'라고 써.\n\n"
)

DAILY_DIGEST_MORNING_PART2 = (
    "[PART 2 : 모닝 시장 구조 분석]\n"
    "PART 1 마지막 이슈 뒤에 빈 줄 2개를 두고, 'PART 2: 모닝 시장 구조 분석'이라는 제목 줄을 "
    "쓴 뒤 아래 섹션들을 순서대로 작성해. 반드시 아래에 제공되는 정량 데이터의 실제 수치를 "
    "인용해서 서술하고(예: '나스닥 100이 X.XX% 상승해 YY,YYY 부근'처럼), 수치 없이 뭉뚱그린 "
    "서술은 하지 마. 정량 데이터가 없는 항목만 텔레그램 텍스트로 서술해.\n\n"
    "🌡️ 시장 온도계 (필수)\n"
    "미국장 마감 및 밤사이 지표를 종합해. S&P500/나스닥100/러셀2000/VIX/미국 10년물/10년 BEI/"
    "10년 실질금리/2년물/DXY/원달러를 반드시 포함하고, 오늘 시장을 Risk-on/중립/경계/Risk-off "
    "중 하나로 규정해. 단, 한 지표만으로 판단하지 말고 지수 방향·VIX·달러·명목/실질금리·중소형주"
    " 상대수익률을 종합해서 판단해. 그리고 '오늘 시장을 실제로 지배한 핵심 변수'가 무엇인지 "
    "반드시 짚어줘.\n\n"
    "⚡ Energy Pulse (필수)\n"
    "오늘 에너지 시장에서 가장 중요한 변화 2~3개만 선정해서 [가격 변화]→[원인]→[공급/수요 "
    "요인]→[재고/정제마진/생산 확인]→[인플레이션/금리 영향]→[주식시장 영향] 순으로 분석해. "
    "Oil(재고/OPEC+/중동·러시아/미국생산/정제마진/선물곡선)과 Gas(날씨/재고/LNG/발전수요/유럽 "
    "수력·원전/LNG시설)는 반드시 분리해서 써. 마지막 줄에 '📌 오늘 에너지 시장의 핵심: '으로 "
    "시작해서 오늘 가격을 움직인 가장 중요한 변수가 공급/수요/날씨/지정학/달러·금리 중 무엇인지"
    " 한 문장으로 정리해.\n\n"
    "🔗 금리 × 에너지 연결 (필수)\n"
    "유가-미국10Y, 유가-BEI, 유가-실질금리, TTF-유럽금리, 에너지-달러, 에너지-성장주, 에너지-"
    "경기민감주 관계를 확인하고, 특히 미국 10년물 움직임이 BEI 주도인지 실질금리 주도인지 "
    "구분해줘. 실제로 연결되지 않으면 '오늘은 에너지와 금리 사이의 뚜렷한 연결은 관찰되지 "
    "않는다'라고 명시해(억지로 인과관계를 만들지 마).\n\n"
    "⚡ AI · 전력 · 에너지 Chain (조건부 — 빅테크/하이퍼스케일러 데이터센터 CAPEX, 전력수요 "
    "전망, 발전용 천연가스, 원전, 가스터빈, 전력망, 변압기, 전력 유틸리티, PPA 관련 이슈가 "
    "실제로 있는 날에만 작성. 없으면 제목째 완전히 생략)\n\n"
    "🔀 시장 괴리 체크 (조건부 — 유가↑/에너지주↓, 금리↑/나스닥↑, DXY↑/EM↑, TTF↑/유럽산업주↑, "
    "실질금리↑/금↑ 처럼 통상적인 Cross-Asset 관계가 깨진 날에만, 실적/포지셔닝/개별기업뉴스/"
    "경기전망/정책/기술적 요인 등으로 이유를 설명하며 작성. 특이점 없으면 완전히 생략)\n\n"
    "🌾 2차 인플레이션 Watch (조건부 — 천연가스/비료/밀/옥수수/대두/해상운임 중 실제 의미 있는 "
    "움직임이 있을 때만, 에너지→비료→곡물→식품물가 전이 징후를 분석. 단순히 에너지 가격이 "
    "올랐다는 이유만으로 쓰지 마. 없으면 완전히 생략)\n\n"
    "🌐 글로벌 로테이션 & 오늘 한국장 체크포인트 (필수, 마지막 섹션)\n"
    "Mega Cap↔Small Cap, Growth↔Value, Tech↔Energy, Cyclical↔Defensive, 미국↔유럽↔아시아 "
    "로테이션 양상을 관찰하되, 가격 상대수익률과 실제 자금흐름(ETF Flow/포지셔닝/수급)을 엄격히"
    " 구분해 — 실제 Flow 데이터가 없으면 '자금 유입/이탈'이라 단정하지 말고 '상대적 강세', "
    "'로테이션 양상', '선호 이동 가능성'으로 표현해. 미국 시장의 상대강도 → 원/달러 → 외국인 "
    "수급 가능성 → 오늘 국내 핵심 섹터(반도체/정유/화학/조선/전력기기/원전/방산 중 당일 연결성"
    " 높은 것만 압축) 순으로 연결해서 마무리해.\n\n"
)

DAILY_DIGEST_EVENING_PART1 = (
    "[PART 1 : 아시아 장중 & 오후 핵심 이슈]\n"
    "15:30 KST 한국장 마감 직후 기준이야. 오전 브리핑을 반복하지 말고, 아시아 장중 반응 + 오전"
    " 전망 복기 + 유럽 개장 전후 흐름 + 오늘 밤 미국장 시나리오에 집중해. 장중 아시아/중국 "
    "매크로, 지정학 속보, 산업 뉴스, 에너지 가격 변화 중 중요한 것만 3~4개 골라줘. 각 이슈마다"
    " 아래 형식으로:\n"
    "   - 핵심 뉴스 요약 1~2문장 (글머리 기호 '-' 사용)\n"
    "   그 바로 아래 줄에 공백 4칸(스페이스 4개)으로 들여쓴 뒤 '💡 시장 영향: '으로 시작하는 "
    "문장 1개. 확실한 연관성이 없으면 인사이트 줄을 생략해도 돼.\n"
    "   이슈 묶음 사이는 빈 줄 1개로 구분해줘.\n"
    "PART 1 제목 줄은 정확히 'PART 1: 아시아 장중 & 오후 핵심 이슈'라고 써.\n\n"
)

DAILY_DIGEST_EVENING_PART2 = (
    "[PART 2 : 애프터눈 시장 구조 & 미장 프리뷰]\n"
    "PART 1 마지막 이슈 뒤에 빈 줄 2개를 두고, 'PART 2: 애프터눈 시장 구조 & 미장 프리뷰'라는 "
    "제목 줄을 쓴 뒤 아래 섹션들을 순서대로 작성해. 반드시 정량 데이터의 실제 수치를 인용해서 "
    "서술해.\n\n"
    "🌡️ 아시아 시장 온도계 & 오전 뷰 복기 (필수)\n"
    "코스피/코스닥/일본/중국/홍콩, 원/달러, 외국인·기관 수급(실제 Flow 데이터가 없으면 상대강세"
    "로만 표현)을 종합해. 이어서 같은 섹션 안에 '오전 전망 검증: '으로 시작하는 문장을 추가해서"
    " 오전 브리핑에서 예상한 금리·에너지발 한국장 반응이 실제로 나타났는지 1~2문장으로 복기하고"
    "(예상과 달랐다면 무엇이 예상보다 강한 변수였는지도 설명), 오전 브리핑 내용을 알 수 없으면 "
    "이 문장은 생략해.\n\n"
    "⚡ Energy & Europe Pulse (필수)\n"
    "아시아 시간대 Brent/WTI/Henry Hub/TTF/DXY 변화를 [가격 변화]→[원인]→[공급/수요 요인]→"
    "[재고/정제마진/생산 확인]→[인플레이션/금리 영향]→[주식시장 영향] 순으로 분석해(Oil/Gas "
    "반드시 분리). 유럽 현물시장이 아직 개장 전이면 유럽 지수선물/TTF/유로/유럽 국채금리를 "
    "기준으로, 개장 후라면 유럽 현물지수·섹터 반응을 추가해(서머타임 여부를 감안해 실제 개장 "
    "여부를 판단해). 마지막 줄에 '📌 오늘 에너지 시장의 핵심: '으로 시작하는 한 문장을 추가해.\n\n"
    "🔗 장중 금리 × 에너지 × 환율 연결 (필수)\n"
    "미국 국채금리, BEI/실질금리, DXY, 원/달러, Brent/TTF의 장중 동조 또는 괴리를 분석하고, "
    "미국 10년물 움직임이 BEI 주도인지 실질금리 주도인지 구분해. 뚜렷한 연결이 없으면 그렇다고"
    " 명시해.\n\n"
    "⚡ AI · 전력 · 에너지 Chain (조건부 — 실제 이슈가 있는 날에만. 없으면 완전히 생략)\n\n"
    "🔀 시장 괴리 체크 (조건부 — Cross-Asset 관계가 깨진 날에만. 없으면 완전히 생략)\n\n"
    "🌾 2차 인플레이션 Watch (조건부 — 실제 의미 있는 움직임이 있을 때만. 없으면 완전히 생략)\n\n"
    "🌙 오늘 밤 미장 핵심 관전 포인트 (필수, 마지막 섹션)\n"
    "CPI/PCE/고용/JOLTS/Fed 발언/국채입찰/EIA 원유재고/EIA 가스재고/OPEC+/중동 뉴스/기업실적 중"
    " 2~4개만 선정해. 각 이벤트마다 ①시장 컨센서스 ②이전치 ③예상 상회 시 ④예상 하회 시를 "
    "기준으로 에너지→BEI→실질금리→달러→주식 반응 시나리오를 간결하게 제시해. 시장 컨센서스를 "
    "확인하지 못했으면 임의의 수치 기준을 만들지 말고 방향성 시나리오만 써. 이 섹션 제목 줄은 "
    "정확히 '🌙 오늘 밤 미장 핵심 관전 포인트'라고 써(화살표는 우리 쪽에서 자동으로 붙이니 "
    "너는 붙이지 마).\n\n"
)


def generate_daily_digest(
    entries_text: str, count: int, quant_data_text: Optional[str] = None, digest_time: str = "evening",
) -> Optional[str]:
    """하루 동안 쌓인 정성적 정보(텔레그램 메시지)와 정량적 시장 데이터(주식/VIX/환율/에너지는
    yfinance, 금리(명목/실질/BEI)는 FRED)를 결합해서, 금리축·에너지축을 하나의 프레임으로 엮는
    'Daily Market Strategist' 브리핑을 생성.
    digest_time: "morning"(8시, 어젯밤 미국장이 막 끝나고 한국장이 곧 열림, 밤사이 이슈 중심) 또는
    "evening"(18시, 오늘 한국장이 막 끝나고 미국장이 곧 열림, 아시아 장중 반응 + 오전 전망 복기 +
    미장 프리뷰 중심)."""
    quant_section = (
        f"[오늘의 정량 데이터]\n{quant_data_text}\n\n"
        if quant_data_text
        else "[오늘의 정량 데이터]\n(조회 실패 또는 미제공 — 아래 텔레그램 텍스트 정보만으로 분석)\n\n"
    )
    part1, part2 = (
        (DAILY_DIGEST_MORNING_PART1, DAILY_DIGEST_MORNING_PART2) if digest_time == "morning"
        else (DAILY_DIGEST_EVENING_PART1, DAILY_DIGEST_EVENING_PART2)
    )

    prompt = (
        f"아래는 오늘 하루 동안 여러 텔레그램 채널에서 수집된 주식/투자 관련 정보 {count}건과, "
        "금리·에너지·주식·환율 정량 데이터야. 이 둘을 종합해서 데일리 마켓 노트를 작성해줘.\n\n"
        "[프롬프트 안내 — 이 대괄호 섹션들은 지시사항 구분용이니 실제 출력에는 절대 포함하지 마]\n\n"
        + DAILY_DIGEST_ROLE_INSTRUCTION
        + part1
        + part2
        + DAILY_DIGEST_WRITING_PRINCIPLES
        + DAILY_DIGEST_FORMAT_GUARD
        + TRUMP_WORDING_INSTRUCTION + "\n\n"
        + quant_section
        + f"[오늘의 정보 모음]\n{entries_text}"
    )
    return call_llm(prompt, max_tokens=3000) or None


INDICATOR_COUNTRY_RE = re.compile(r"국가\s*[:：]\s*([^\s*\n]+)")
DART_TICKER_RE = re.compile(r"\bA\d{6}\b")


def extract_indicator_country(text: str) -> Optional[str]:
    """'[경제지표]' 류 메시지에서 '국가: XX' 필드를 추출. 없으면 None.
    다른 나라 경제지표는 형식(템플릿)이 같아도 절대 같은 정보로 취급하면 안 되므로,
    이 값이 서로 다르면 유사도 계산 자체를 건너뛰는 데 사용됨."""
    m = INDICATOR_COUNTRY_RE.search(text)
    return m.group(1) if m else None


def extract_dart_ticker(text: str) -> Optional[str]:
    """DART(전자공시) 자동 발신 메시지에서 종목코드(예: A003230)를 추출. 없으면 None.
    '기업명:', '공시링크:' 등 틀 문구가 반복돼서 서로 다른 회사 공시인데도 유사도가
    높게 나올 수 있어, 종목코드가 다르면 아예 비교 대상에서 제외하는 데 사용됨."""
    m = DART_TICKER_RE.search(text)
    return m.group(0) if m else None


BRIEFING_DATE_RE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
AI_SIGNAL_COMPANY_RE = re.compile(r"\[AI시그널\]\s*([^,]+),")


def extract_briefing_date(text: str) -> Optional[str]:
    """'2026년 7월 9일 프리마켓 뉴스' 같은 정기 일일 브리핑 제목에서 날짜를 추출. 없으면 None.
    같은 채널의 매일 반복되는 시황 브리핑은 어휘(AI, 반도체, 연준, 국제유가 등)가 겹치기
    쉬워서, 제목의 날짜가 다르면(=다른 날짜의 브리핑이면) 아예 비교 대상에서 제외하는 데 사용.
    본문 중간의 다른 날짜 언급과 혼동되지 않도록 텍스트 맨 앞부분에서만 찾음."""
    m = BRIEFING_DATE_RE.search(text[:100])
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def extract_ai_signal_company(text: str) -> Optional[str]:
    """'[AI시그널] 리걸줌, 저항선 근처...' 같은 자동 발신 AI 종목분석 메시지에서 종목명을 추출.
    없으면 None. 이 서비스는 종목이 달라도 링크가 전부 동일한 서비스 페이지 URL을 공유해서,
    URL 매칭만으로는 서로 다른 종목의 분석글이 중복으로 오판될 수 있어 종목명이 다르면
    아예 비교 대상에서 제외하는 데 사용됨."""
    m = AI_SIGNAL_COMPANY_RE.search(text)
    return m.group(1).strip() if m else None


def extract_political_section_titles(text: str) -> Optional[frozenset]:
    """'📍호르무즈 해협', '📍이란 군사 대응'처럼 📍 섹션 제목들을 집합으로 추출.
    섹션이 하나도 없으면 None. 같은 채널이 '트럼프 발언 정리'류 메시지를 같은 날 여러 번
    올릴 때, 다루는 섹션 주제가 완전히 다르면(교집합 없음) 공통 어휘(트럼프, 이란 등)가 많아도
    절대 같은 내용이 아니므로 비교 대상에서 제외하는 데 사용됨."""
    titles = frozenset(m.strip() for m in re.findall(r"📍([^\n]+)", text))
    return titles or None


BOND_TABLE_TYPE_RE = re.compile(r"(\d+년물|\d+년만기|국채금리\s*전산장\s*마감가)")


def extract_bond_table_type(text: str) -> Optional[str]:
    """'주요국 2년물 국채 수익률 비교', '[美 국채금리 전산장 마감가]'처럼 연합인포의 국채
    관련 자동 발신 표에서 다루는 구체적인 만기/종류(예: '2년물', '10년물', '전산장 마감가')를
    추출. 이런 표들은 국가 목록이나 컬럼 구조가 거의 동일해서 키워드 유사도가 매우 높게
    나오지만(예: '2년물 비교' vs '10년물 비교'는 국가명·헤더가 전부 동일), 실제로는 완전히
    다른 만기의 수익률 데이터를 담고 있어 이 값이 다르면 절대 같은 정보가 아님."""
    m = BOND_TABLE_TYPE_RE.search(text[:80])
    return m.group(1) if m else None


EXPORT_DATA_PRODUCT_RE = re.compile(r"\d+일치\s+(.+?)\s*잠정\s*수출\s*데이터")


def extract_export_data_product(text: str) -> Optional[str]:
    """'26년 7월 20일치 라면 잠정 수출 데이터'처럼 매달 반복되는 품목별 수출 데이터
    리포트에서 다루는 품목(예: '라면', '기초화장용')을 추출. 이런 리포트들은 최근 18개월치
    날짜 라벨('1월'~'12월', '20일치', '25년/26년' 등)이 거의 그대로 반복돼서 전체 키워드의
    대부분을 차지하며, 심지어 자동확정 구간까지 유사도가 치솟을 수 있음. 하지만 실제로는
    완전히 다른 품목의 수출액 데이터라, 이 값이 다르면 절대 같은 정보가 아님."""
    m = EXPORT_DATA_PRODUCT_RE.search(text[:80])
    return m.group(1).strip() if m else None


DEEP_ANALYSIS_REPORT_TIME_RE = re.compile(r"텔레그램\s*심층\s*분석\s*Report\s*\((\d{1,2}시\s*\d{1,2}분)\)\s*기준")


def extract_deep_analysis_report_time(text: str) -> Optional[str]:
    """'📝 텔레그램 심층 분석 Report (15시 07분) 기준'처럼 하루 여러 번(예: 15시, 19시) 자동
    발송되는 센티먼트 분석 리포트에서 기준 시각을 추출. 이런 리포트는 제목/섹션 이모지
    (🌡️/💡/🗣️/🔥/🎉/ℹ️)와 하단 서명이 매번 고정 문구로 반복되고 SK하이닉스·삼성전자·반도체
    같은 상위 키워드도 자주 겹쳐서, 실제 센티먼트 점수·종목·인기글이 전혀 다른 리포트끼리도
    유사도가 매우 높게 나올 수 있음. 기준 시각이 다르면(=다른 시점의 리포트면) 절대 같은
    정보가 아니므로, 이 값이 다르면 비교 대상에서 제외하는 데 사용됨."""
    m = DEEP_ANALYSIS_REPORT_TIME_RE.search(text[:60])
    return m.group(1) if m else None


US_EARNINGS_PRESS_TICKER_RE = re.compile(r"(?:NASDAQ|NYSE)\s*[:：]\s*([A-Z]{1,6})")


def extract_us_earnings_press_ticker(text: str) -> Optional[str]:
    """'미드랜드 스테이츠 뱅코프(MIDLAND STATES BANCORP, INC., NASDAQ:MSBI)'처럼 데이터투자/
    이데일리FX가 자동 생성하는 미국 상장기업 실적 리포트에서 티커를 추출. 회사가 완전히
    달라도 '2분기 순이익/EPS/이자수익/비이자수익/총자산/총예금' 같은 재무제표 항목 구조와
    안내 문구가 거의 동일해서(STOPWORDS만으론 못 걸러짐) 유사도가 높게 나올 수 있음. 티커가
    다르면 절대 같은 정보가 아니므로 비교 대상에서 제외하는 데 사용됨."""
    m = US_EARNINGS_PRESS_TICKER_RE.search(text[:500])
    return m.group(1) if m else None


REPORT_AUTHOR_TAG_RE = re.compile(r"^\[([^\[\]]{5,60})\]")


def extract_report_author_tag(text: str) -> Optional[str]:
    """'[키움증권 미국 전략/주식 김승혁]', '[8/3, Kiwoom Weekly, 키움 전략 한지영]'처럼 증권사
    리서치 메시지 맨 앞에 붙는 '소속·담당자 또는 리포트명' 태그를 추출. 서로 다른 애널리스트가
    같은 주의 매크로 캘린더(같은 날짜의 ISM·고용지표·실적 발표 일정)를 각자 다른 리포트(예:
    '미국 주식 Weekly' vs 'ETF Weekly')에서 다루면, 실제로는 다른 리포트인데도 공통 이벤트·
    티커 키워드가 많이 겹쳐서 유사도가 높게 나올 수 있음. 이 태그가 다르면(=다른 담당자/다른
    리포트면) 절대 같은 정보가 아니므로 비교 대상에서 제외하는 데 사용됨. 5자 미만의 짧은
    범용 카테고리 태그(예: '[속보]', '[단독]')까지 다르게 취급하면 오히려 서로 다른 채널의
    진짜 중복을 놓칠 수 있어 최소 길이를 둠."""
    m = REPORT_AUTHOR_TAG_RE.match(text.strip())
    return m.group(1).strip() if m else None


SUM_REPORT_HASHTAG_RE = re.compile(r"#(\S+)\s*\n+\s*🔗\s*큐리어스IR")


def extract_sum_report_hashtag(text: str) -> Optional[str]:
    """'[썸❤️리포트] ... #일진전기 🔗큐리어스IR 텔레그램 링크'처럼 큐리어스IR이 발행하는
    종목 리포트에서 제목 바로 위 해시태그(종목/테마명)를 추출. 이 채널은 네이버 링크
    미리보기(우리 쪽이 텍스트 보강용으로 자동 첨부하는 '[링크 미리보기 정보]' 설명)가
    종목과 무관하게 'OPM/SOTP 정의' 같은 공통 용어 해설로 시작해서, 실제 종목이 완전히
    달라도(예: 일진전기 vs 하나머티리얼즈) 유사도가 높게 나올 수 있음. 해시태그가 다르면
    절대 같은 리포트가 아니므로 비교 대상에서 제외하는 데 사용됨."""
    m = SUM_REPORT_HASHTAG_RE.search(text)
    return m.group(1).strip() if m else None


def purge_old_entries():
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=DEDUP_WINDOW_MINUTES)
    global recent_buffer
    recent_buffer = [m for m in recent_buffer if m["time"] >= cutoff]
    seen_hashes.clear()
    seen_hashes.update(m["hash"] for m in recent_buffer)
    purge_old_entries_from_db(cutoff)


def is_duplicate(text: str, source_label: str = "", source_link: str = "") -> Optional[dict]:
    """중복이 아니면 None 반환. 중복이면 매칭된 '이전 항목'(text/source_label/source_link 포함
    dict)을 반환 — 호출부에서 이 정보로 '이전 제목 + 원문 링크'를 안내할 수 있음."""
    purge_old_entries()

    preview = make_preview(text, 40)  # 로그에 찍을 짧은 미리보기

    normalized = normalize(text)
    urls = extract_urls(text)

    if not normalized and not urls:
        return None  # 텍스트도 URL도 없는 메시지(이미지 단독 등)는 일단 통과시킴

    h = exact_hash(normalized)
    if normalized and h in seen_hashes:
        matched = next((e for e in recent_buffer if e["hash"] == h), None)
        log.info(f"완전 동일 메시지 → 스킵: {preview!r}")
        return matched or {"text": text, "source_label": source_label, "source_link": source_link}

    kw, num = extract_signature(normalized)
    current_country = extract_indicator_country(text)
    current_ticker = extract_dart_ticker(text)
    current_briefing_date = extract_briefing_date(text)
    current_ai_signal_company = extract_ai_signal_company(text)
    current_political_sections = extract_political_section_titles(text)
    current_bond_table_type = extract_bond_table_type(text)
    current_export_data_product = extract_export_data_product(text)
    current_deep_analysis_time = extract_deep_analysis_report_time(text)
    current_us_earnings_ticker = extract_us_earnings_press_ticker(text)
    current_report_author_tag = extract_report_author_tag(text)
    current_sum_report_hashtag = extract_sum_report_hashtag(text)

    # 같은 기사/링크를 공유하는 기록들 중 텍스트 유사도가 가장 높은 것을 추적
    # (URL은 같아도 문구가 완전히 다르면 "같은 기사에 다른 의견"일 수 있어 LLM으로 재확인)
    url_match_best_score = -1.0
    url_match_best_entry = None

    # 예전엔 "가장 점수 높은 후보 딱 1개"만 추적했는데, 우연히 무관한 메시지가 점수가 더 높으면
    # 진짜 같은 내용인 후보가 밀려나서 LLM한테 확인받을 기회조차 없이 새 정보로 등록되는 문제가
    # 있었음 (예: 다른 채널의 같은 속보가 무관한 다른 기사에 밀려 비교조차 안 된 사례).
    # 이제 THRESHOLD_LOW를 넘는 후보들을 전부 점수순으로 모아뒀다가, 상위 몇 개를 순서대로
    # LLM에게 확인시킴 (하나라도 "같은 정보"면 그걸로 확정, 다 아니면 새 정보로 통과).
    candidates = []
    best_score = 0.0
    best_match_entry = None
    for entry in recent_buffer:
        entry_country = extract_indicator_country(entry["text"])
        if current_country and entry_country and current_country != entry_country:
            # 예: '중국 CPI'와 '독일 수출'처럼 형식은 같아도 국가가 다르면
            # 절대 같은 정보가 아니므로, 점수 계산 없이 이 항목은 비교 대상에서 제외
            continue
        entry_ticker = extract_dart_ticker(entry["text"])
        if current_ticker and entry_ticker and current_ticker != entry_ticker:
            # DART 공시 형식은 같아도 종목코드(A003230 등)가 다르면 다른 회사 공시이므로 제외
            continue
        entry_briefing_date = extract_briefing_date(entry["text"])
        if current_briefing_date and entry_briefing_date and current_briefing_date != entry_briefing_date:
            # '7월 9일 프리마켓 뉴스'와 '7월 8일 프리마켓 뉴스'처럼 같은 채널의 정기 브리핑이라도
            # 날짜가 다르면 절대 같은 정보가 아니므로 제외
            continue
        entry_ai_signal_company = extract_ai_signal_company(entry["text"])
        if current_ai_signal_company and entry_ai_signal_company and current_ai_signal_company != entry_ai_signal_company:
            # [AI시그널] 종목분석은 종목이 달라도 같은 서비스 URL을 공유해서 URL매칭이 오작동할
            # 수 있어, 종목명이 다르면 URL이 같아도 절대 같은 정보가 아니므로 제외
            continue
        entry_political_sections = extract_political_section_titles(entry["text"])
        if (
            current_political_sections
            and entry_political_sections
            and not (current_political_sections & entry_political_sections)
        ):
            # '트럼프 발언 정리'류 메시지가 같은 날 여러 번 올라올 수 있는데, 다루는 📍 섹션
            # 주제가 완전히 다르면(교집합 없음) 공통 어휘(트럼프, 이란 등)가 많아도 절대 같은
            # 내용이 아니므로 제외
            continue
        entry_bond_table_type = extract_bond_table_type(entry["text"])
        if current_bond_table_type and entry_bond_table_type and current_bond_table_type != entry_bond_table_type:
            # '주요국 2년물 국채 수익률 비교'와 '주요국 10년물 국채 수익률 비교'는 국가명·
            # 컬럼 헤더가 거의 동일해서 키워드 유사도가 매우 높게 나오지만, 만기가 다르면
            # 절대 같은 정보가 아니므로 제외
            continue
        entry_export_data_product = extract_export_data_product(entry["text"])
        if (
            current_export_data_product
            and entry_export_data_product
            and current_export_data_product != entry_export_data_product
        ):
            # '라면 잠정 수출 데이터'와 '기초화장용 잠정 수출 데이터'는 반복되는 월별 날짜
            # 라벨이 거의 동일해서 유사도가 자동확정 구간까지 치솟을 수 있지만, 품목이 다르면
            # 절대 같은 정보가 아니므로 제외
            continue
        entry_deep_analysis_time = extract_deep_analysis_report_time(entry["text"])
        if (
            current_deep_analysis_time
            and entry_deep_analysis_time
            and current_deep_analysis_time != entry_deep_analysis_time
        ):
            # '(15시 07분) 기준'과 '(19시 11분) 기준'은 고정 섹션 이모지·서명이 반복되고
            # 상위 키워드(반도체, 삼성전자 등)도 자주 겹쳐서 유사도가 높게 나올 수 있지만,
            # 기준 시각이 다르면 센티먼트 점수·종목·인기글이 전혀 다른 별개 리포트이므로 제외
            continue
        entry_us_earnings_ticker = extract_us_earnings_press_ticker(entry["text"])
        if (
            current_us_earnings_ticker
            and entry_us_earnings_ticker
            and current_us_earnings_ticker != entry_us_earnings_ticker
        ):
            # 데이터투자/이데일리FX의 미국 실적 리포트는 회사가 달라도 '순이익/EPS/이자수익/
            # 총자산/총예금' 같은 재무제표 항목 구조가 거의 동일해서 유사도가 높게 나올 수
            # 있지만, 티커가 다르면 완전히 다른 회사의 실적이므로 제외
            continue
        entry_report_author_tag = extract_report_author_tag(entry["text"])
        if (
            current_report_author_tag
            and entry_report_author_tag
            and current_report_author_tag != entry_report_author_tag
        ):
            # 서로 다른 애널리스트/리포트(예: '[키움증권 미국 전략/주식 김승혁]' vs
            # '[키움 글로벌 ETF 김진영]')가 같은 주의 매크로 캘린더(같은 실적·지표 일정)를
            # 각자 다루면 공통 키워드가 많이 겹칠 수 있지만, 담당자/리포트명 태그가 다르면
            # 완전히 다른 리포트이므로 제외
            continue
        entry_sum_report_hashtag = extract_sum_report_hashtag(entry["text"])
        if (
            current_sum_report_hashtag
            and entry_sum_report_hashtag
            and current_sum_report_hashtag != entry_sum_report_hashtag
        ):
            # 큐리어스IR 리포트는 네이버 링크 미리보기 설명이 종목과 무관하게 'OPM/SOTP
            # 정의' 같은 공통 용어 해설로 시작해서, 실제 종목이 완전히 달라도(예: 일진전기
            # vs 하나머티리얼즈) 유사도가 높게 나올 수 있지만, 해시태그(종목/테마명)가
            # 다르면 완전히 다른 리포트이므로 제외
            continue
        score = similarity_score(kw, num, entry["keywords"], entry["numbers"])
        if score >= THRESHOLD_LOW:
            candidates.append((score, entry))
        if score > best_score:
            best_score = score
            best_match_entry = entry
        if urls and entry.get("urls") and (urls & entry["urls"]) and score > url_match_best_score:
            url_match_best_score = score
            url_match_best_entry = entry
    candidates.sort(key=lambda x: x[0], reverse=True)

    if url_match_best_entry is not None:
        matched_preview = make_title_preview(url_match_best_entry["text"], 40)
        shared_urls = urls & url_match_best_entry.get("urls", set())
        if url_match_best_score >= URL_MATCH_SCORE_THRESHOLD:
            log.info(
                f"동일 URL 공유({sorted(shared_urls)}) + 문구도 유사({url_match_best_score:.2f}) "
                f"→ 중복으로 판단, 스킵: {preview!r} (기존: {matched_preview!r})"
            )
            return url_match_best_entry
        else:
            log.info(
                f"동일 URL({sorted(shared_urls)})이지만 문구 유사도 낮음({url_match_best_score:.2f}) "
                f"→ 같은 의견인지 LLM에게 재확인: {preview!r} vs {matched_preview!r}"
            )
            if ask_llm_same_info(text, url_match_best_entry["text"]):
                log.info(f"LLM 판단: 같은 내용/의견 → 스킵: {preview!r} (기존: {matched_preview!r})")
                return url_match_best_entry
            else:
                log.info(f"LLM 판단: 같은 기사지만 다른 의견/코멘트 → 새 정보로 전달: {preview!r}")

    has_enough_keywords = (
        best_match_entry is not None
        and len(kw) >= MIN_KEYWORDS_FOR_AUTO_DUP
        and len(best_match_entry["keywords"]) >= MIN_KEYWORDS_FOR_AUTO_DUP
    )
    size_ratio_ok = True
    if best_match_entry is not None:
        smaller_kw = min(len(kw), len(best_match_entry["keywords"]))
        larger_kw = max(len(kw), len(best_match_entry["keywords"]))
        size_ratio_ok = smaller_kw > 0 and (larger_kw / smaller_kw) <= MAX_AUTO_DUP_SIZE_RATIO

    if best_score >= THRESHOLD_HIGH and has_enough_keywords and size_ratio_ok:
        matched_preview = make_title_preview(best_match_entry["text"], 40)
        log.info(
            f"키워드 유사도 높음({best_score:.2f}) → 중복으로 판단, 스킵: "
            f"{preview!r} (기존: {matched_preview!r})"
        )
        return best_match_entry

    # THRESHOLD_LOW를 넘는 후보들을 점수 높은 순으로 최대 MAX_LLM_DUP_CANDIDATES개까지
    # 순서대로 LLM에게 확인. 우연히 무관한 후보가 1등이어도, 그다음 순위 후보 중 진짜
    # 같은 내용이 있으면 놓치지 않고 잡아냄.
    for score, entry in candidates[:MAX_LLM_DUP_CANDIDATES]:
        matched_preview = make_title_preview(entry["text"], 40)
        if score >= THRESHOLD_HIGH:
            log.info(
                f"키워드 유사도는 높지만({score:.2f}) 캡션이 짧아 자동 확정 대신 "
                f"LLM에게 재확인 요청: {preview!r} vs {matched_preview!r}"
            )
        else:
            log.info(f"애매한 유사도({score:.2f}) → LLM에게 재확인 요청: {preview!r} vs {matched_preview!r}")
        if ask_llm_same_info(text, entry["text"]):
            log.info(f"LLM 판단: 같은 정보 → 스킵: {preview!r} (기존: {matched_preview!r})")
            return entry
        else:
            log.info(f"LLM 판단: 다른 정보 → 다음 후보 확인 (또는 새 정보로 전달): {preview!r}")

    # 새로운 정보로 등록 (메모리 + DB 양쪽에 저장 → 재시작해도 유지됨)
    new_entry = {
        "text": text,
        "keywords": kw,
        "numbers": num,
        "urls": urls,
        "source_label": source_label,
        "source_link": source_link,
        "hash": h,
        "time": datetime.now(timezone.utc),
    }
    if normalized:
        seen_hashes.add(h)
    recent_buffer.append(new_entry)
    save_entry_to_db(new_entry)
    return None


async def send_media_to_summary_chat(
    client, message, caption: str, summary_entity=None, source_label: str = "출처 미상"
) -> Optional[int]:
    """텍스트(캡션) 없는 이미지/미디어 메시지를 DIGEST_CHAT에 그대로 전송.
    봇 토큰이 있으면 봇 명의로(다운로드 후 재업로드), 없으면 유저봇으로 직접 보냄.
    텍스트 전용 알림(send_bot_notification)과 동일하게 인용구(blockquote) 서식을 입혀서
    시각적으로 통일감 있게 보이도록 함. event가 아닌 message를 직접 받아서, 재전송 큐에서
    원본을 다시 조회해온 메시지로도 그대로 재사용할 수 있게 함.
    성공하면 생성된 메시지 id, 실패하면 None."""
    real_media_present = message.media is not None and not isinstance(message.media, MessageMediaWebPage)
    if not real_media_present:
        return None

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if TELEGRAM_BOT_TOKEN:
        tmp_path = None
        try:
            # 메모리에 통째로 올리는 대신 임시 파일로 다운로드 → 디스크에서 스트리밍 업로드.
            # 앨범/큰 파일이 몰릴 때 메모리 사용량이 순간적으로 튀는 걸 줄이기 위함
            # (SIGKILL로 강제 종료됐던 사례의 재발 방지 차원).
            tmp_fd, tmp_path = tempfile.mkstemp(prefix="tg_media_", dir=TEMP_MEDIA_DIR)
            os.close(tmp_fd)
            await client.download_media(message, file=tmp_path)
            styled_caption = f"<blockquote>{html.escape(caption[:1000])}</blockquote>"
            with open(tmp_path, "rb") as f:
                if has_real_photo(message):
                    resp = requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                        data={"chat_id": DIGEST_CHAT, "caption": styled_caption, "parse_mode": "HTML"},
                        files={"photo": ("photo.jpg", f)},
                        timeout=30,
                    )
                else:
                    filename = getattr(getattr(message, "file", None), "name", None) or "file"
                    resp = requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                        data={"chat_id": DIGEST_CHAT, "caption": styled_caption, "parse_mode": "HTML"},
                        files={"document": (filename, f)},
                        timeout=60,
                    )
            if resp.status_code == 200:
                return resp.json()["result"]["message_id"]
            if resp.status_code == 413:
                # 봇 API는 업로드 용량이 20MB로 제한돼 있지만, 유저봇 계정으로는 최대 2GB까지
                # 보낼 수 있음 — 대용량 동영상 등은 재시도해봐야 항상 같은 이유로 실패하므로,
                # 재전송 큐로 넘기지 않고 여기서 바로 유저봇 경로로 폴백함.
                log.warning(
                    f"봇 API 업로드 용량 제한(413) 초과 [채널: {source_label}, 시각: {now_str}] "
                    f"→ 유저봇 계정으로 직접 전송 시도"
                )
                try:
                    sent_msg = await client.send_file(
                        summary_entity, message, caption=styled_caption, parse_mode="html"
                    )
                    return sent_msg.id
                except Exception as e:
                    log.warning(f"유저봇 폴백 전송도 실패 [채널: {source_label}, 시각: {now_str}]: {e}")
                    return None
            log.warning(
                f"봇으로 이미지/미디어 전송 실패 [채널: {source_label}, 시각: {now_str}]: "
                f"{resp.status_code} {resp.text}"
            )
        except Exception as e:
            log.warning(f"봇으로 이미지/미디어 전송 중 오류 [채널: {source_label}, 시각: {now_str}]: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError as e:
                    log.warning(f"임시 파일 삭제 실패(무시하고 계속): {tmp_path}: {e}")
        return None
    else:
        try:
            sent_msg = await client.send_file(summary_entity, message, caption=caption)
            return sent_msg.id
        except Exception as e:
            log.warning(f"유저봇으로 이미지/미디어 전송 실패 [채널: {source_label}, 시각: {now_str}]: {e}")
            return None


async def send_album_to_summary_chat(
    client, album_events: list, caption: str, summary_entity=None, source_label: str = "출처 미상"
) -> Optional[int]:
    """같은 앨범(grouped_id)으로 묶인 여러 장의 사진/동영상을 한 번에 묶어서 전송.
    캡션(요약+링크)은 앨범의 첫 번째 항목에만 붙임 (텔레그램 앨범 규칙).
    성공하면 캡션이 붙은 첫 번째 메시지의 id, 실패하면 None."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if TELEGRAM_BOT_TOKEN:
        tmp_paths = []
        open_files = []
        try:
            files = {}
            media_json = []
            for i, ev in enumerate(album_events[:10]):  # 텔레그램 앨범 한도 10개
                msg = ev.message
                if has_real_photo(msg):
                    media_type, ext = "photo", "jpg"
                elif getattr(msg, "video", None):
                    media_type, ext = "video", "mp4"
                else:
                    media_type, ext = "document", "bin"
                # 앨범 최대 10장을 전부 메모리에 동시에 올리면 순간 메모리 사용량이 크게
                # 튈 수 있어서(SIGKILL로 강제 종료됐던 사례 재발 방지), 장당 임시 파일로
                # 디스크에 내려받은 뒤 파일 핸들로 스트리밍 업로드함
                tmp_fd, tmp_path = tempfile.mkstemp(prefix="tg_album_", dir=TEMP_MEDIA_DIR)
                os.close(tmp_fd)
                tmp_paths.append(tmp_path)
                await client.download_media(msg, file=tmp_path)
                f = open(tmp_path, "rb")
                open_files.append(f)

                field = f"file{i}"
                files[field] = (f"{field}.{ext}", f)
                item = {"type": media_type, "media": f"attach://{field}"}
                if i == 0 and caption:
                    item["caption"] = f"<blockquote>{html.escape(caption[:1000])}</blockquote>"
                    item["parse_mode"] = "HTML"
                media_json.append(item)

            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup",
                data={"chat_id": DIGEST_CHAT, "media": json.dumps(media_json)},
                files=files,
                timeout=90,
            )
            if resp.status_code == 200:
                result_list = resp.json()["result"]
                return result_list[0]["message_id"] if result_list else None
            log.warning(
                f"봇으로 앨범 전송 실패 [채널: {source_label}, 시각: {now_str}]: "
                f"{resp.status_code} {resp.text}"
            )
        except Exception as e:
            log.warning(f"봇으로 앨범 전송 중 오류 [채널: {source_label}, 시각: {now_str}]: {e}")
        finally:
            for f in open_files:
                try:
                    f.close()
                except OSError:
                    pass
            for p in tmp_paths:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError as e:
                        log.warning(f"임시 파일 삭제 실패(무시하고 계속): {p}: {e}")
        return None
    else:
        try:
            files_list = [ev.message for ev in album_events]
            sent = await client.send_file(summary_entity, files_list, caption=caption)
            if isinstance(sent, list):
                return sent[0].id if sent else None
            return sent.id
        except Exception as e:
            log.warning(f"유저봇으로 앨범 전송 실패 [채널: {source_label}, 시각: {now_str}]: {e}")
            return None


DIGEST_STRATEGY_PREFIXES = ("🌡️", "⚡", "🔗", "🔀", "🌐", "🌾", "➡️")


def classify_digest_blocks(rebuilt_blocks: list) -> list:
    """정리 블록들을 종류별로 분류: title(PART 1/2 라벨), issue(핵심 이슈, 이슈+인사이트),
    strategy(시장 구조 분석 섹션, 제목+본문), other(그 외 일반 문단)."""
    classified = []
    for block in rebuilt_blocks:
        lines = block.split("\n")
        first = lines[0].strip()
        if first.startswith(("PART 1", "PART 2")) and len(lines) == 1:
            classified.append({"type": "title", "text": first})
        elif first.startswith(DIGEST_STRATEGY_PREFIXES):
            body = "\n".join(lines[1:]).strip()
            classified.append({"type": "strategy", "title": first, "body": body})
        elif first.startswith("- "):
            insight = None
            issue_lines = []
            for line in lines:
                if "💡" in line[:8]:
                    insight = line.strip().lstrip("💡").strip()
                else:
                    issue_lines.append(line)
            issue_text = "\n".join(issue_lines).strip().lstrip("- ").strip()
            classified.append({"type": "issue", "text": issue_text, "insight": insight})
        else:
            classified.append({"type": "other", "text": block})
    return classified


def render_digest_text_bold(classified_blocks: list) -> str:
    """정리를 텔레그램 HTML(굵게 처리된 제목 + 빈 줄 구분)로 렌더링. 이미 html.escape가
    적용된 안전한 문자열을 반환하므로, 이 결과는 그대로 <blockquote> 안에 넣으면 됨."""
    DIVIDER = "────────────────────"
    parts = []
    for b in classified_blocks:
        if b["type"] == "title":
            if b["text"].startswith("PART 2") and parts:
                # PART 1과 PART 2 사이를 시각적으로 확실히 구분
                parts.append(DIVIDER)
            parts.append(f"<b>{html.escape(b['text'])}</b>")
        elif b["type"] == "strategy":
            title_html = f"<b>{html.escape(b['title'])}</b>"
            body_html = html.escape(b["body"]) if b["body"] else ""
            parts.append(f"{title_html}\n{body_html}" if body_html else title_html)
        elif b["type"] == "issue":
            issue_html = f"- {html.escape(b['text'])}"
            if b["insight"]:
                issue_html += f"\n    💡 {html.escape(b['insight'])}"
            parts.append(issue_html)
        else:
            parts.append(html.escape(b["text"]))
    return "\n\n".join(parts)


NUM_PCT_RE = re.compile(r"([+-]?\d[\d,]*\.?\d*)\s*%")
POSITIVE_CONTEXT_WORDS = ("상승", "오른", "올랐", "증가", "급등", "확대")
NEGATIVE_CONTEXT_WORDS = ("하락", "내린", "떨어진", "감소", "급락", "축소")


def colorize_quant_numbers(escaped_text: str) -> str:
    """이미 html.escape()가 적용된 텍스트에서 퍼센트 숫자를 찾아 상승(초록)/하락(빨강)/
    중립(파랑) 색상 span으로 감쌈. 숫자에 부호(+/-)가 명시돼 있으면 그걸 우선 기준으로
    삼고, 없으면 숫자 앞쪽 근처 문맥에서 '상승'/'하락' 같은 단어를 찾아 판단함."""
    def repl(m: re.Match) -> str:
        full, num_str = m.group(0), m.group(1)
        if num_str.startswith("+"):
            cls = "num-pos"
        elif num_str.startswith("-"):
            cls = "num-neg"
        else:
            # 한국어 문장 구조상 '상승/하락' 같은 방향 단어가 숫자 앞("상승한 0.19%")과
            # 뒤("0.19% 하락") 어느 쪽에도 올 수 있어서, 양쪽 다 확인함
            before = escaped_text[max(0, m.start() - 15):m.start()]
            after = escaped_text[m.end():m.end() + 15]
            context = before + after
            if any(w in context for w in POSITIVE_CONTEXT_WORDS):
                cls = "num-pos"
            elif any(w in context for w in NEGATIVE_CONTEXT_WORDS):
                cls = "num-neg"
            else:
                cls = "num-neu"
        return f'<span class="{cls}">{full}</span>'
    return NUM_PCT_RE.sub(repl, escaped_text)


def render_digest_html(
    classified_blocks: list, time_range: str, count: int,
    vix_value: Optional[float] = None, vix_change_pct: Optional[float] = None,
) -> str:
    """정리를 텔레그램에서 문서로 바로 열어볼 수 있는 스타일 있는 HTML 파일로 렌더링.
    vix_value가 주어지면, 그날 실제 VIX 수치를 기준으로 '시장 온도계' 게이지를 매번 새로
    그려서 넣음 (조회 실패 시 게이지 없이 나머지 내용만 렌더링)."""
    issue_html_parts = []
    strategy_html_parts = []
    for b in classified_blocks:
        if b["type"] == "issue":
            insight_block = (
                f'<div class="issue-insight"><span class="icon">💡</span>'
                f'<span class="txt">{html.escape(b["insight"])}</span></div>'
                if b["insight"] else ""
            )
            issue_html_parts.append(
                f'<div class="issue"><div class="issue-text">{html.escape(b["text"])}</div>{insight_block}</div>'
            )
        elif b["type"] == "strategy":
            is_tonight = b["title"].lstrip("➡️ ").startswith("🌙")
            extra_class = " highlight" if is_tonight else ""
            title_class = " tonight" if is_tonight else ""
            title_text = b["title"].lstrip("➡️ ").strip()
            body_paragraphs = "".join(
                f"<p>{colorize_quant_numbers(html.escape(p))}</p>" for p in b["body"].split("\n") if p.strip()
            )
            strategy_html_parts.append(
                f'<div class="strategy{extra_class}"><div class="strategy-title{title_class}">'
                f'{html.escape(title_text)}</div>{body_paragraphs}</div>'
            )

    # 시장 온도계 게이지: VIX 실제 값을 10~40 구간에 매핑해서 마커 위치(%)를 계산.
    # 10 이하는 극도로 안정, 40 이상은 극도로 불안한 구간으로 보고 양 끝을 클램프함.
    pulse_html = ""
    if vix_value is not None:
        marker_pct = max(0.0, min(1.0, (vix_value - 10) / (40 - 10))) * 100
        change_str = f"({vix_change_pct:+.2f}%)" if vix_change_pct is not None else ""
        if vix_value < 20:
            note = "20 미만의 안정적인 구간으로, 시장이 비교적 차분한 상태입니다."
        elif vix_value < 30:
            note = "20~30 구간의 경계 수준으로, 시장에 일정 수준의 긴장감이 감돌고 있습니다."
        else:
            note = "30을 넘는 공포 구간으로, 시장 전반의 불안 심리가 크게 확대된 상태입니다."
        pulse_html = f"""
  <div class="pulse">
    <div class="pulse-top">
      <div class="pulse-label">시장 온도계 · VIX 기준</div>
      <div class="pulse-reading">{vix_value:.2f} {change_str}</div>
    </div>
    <div class="pulse-track">
      <div class="pulse-marker" style="left: {marker_pct:.1f}%;"></div>
    </div>
    <div class="pulse-scale">
      <span>안정</span><span>경계</span><span>공포</span>
    </div>
    <div class="pulse-note">{html.escape(note)}</div>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>정리 ({html.escape(time_range)})</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0e1015; --bg-card: #161922; --border: #262b38;
    --text-primary: #eceef3; --text-secondary: #8d94a6; --text-tertiary: #5c6376;
    --accent-cool: #6fa8ff; --accent-warm: #f2b544;
    --bg-gradient-1: rgba(111,168,255,0.08); --bg-gradient-2: rgba(242,181,68,0.06);
    --insight-bg: rgba(242,181,68,0.06); --insight-border: rgba(242,181,68,0.16);
    --insight-text: #d8cba8;
    --highlight-border: rgba(111,168,255,0.35); --highlight-bg: rgba(111,168,255,0.05);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg);
    background-image: radial-gradient(ellipse 900px 500px at 15% -10%, var(--bg-gradient-1), transparent),
      radial-gradient(ellipse 700px 500px at 100% 0%, var(--bg-gradient-2), transparent);
    color: var(--text-primary); font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    line-height: 1.6; padding: 0 0 60px 0;
  }}
  .wrap {{ max-width: 640px; margin: 0 auto; padding: 32px 20px 0; }}
  .eyebrow {{ font-family: 'IBM Plex Mono', monospace; font-size: 12px; letter-spacing: 0.14em;
    text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 10px; }}
  h1 {{ font-family: 'Fraunces', serif; font-weight: 600; font-size: 30px; line-height: 1.25;
    margin: 0 0 6px; letter-spacing: -0.01em; }}
  .meta {{ font-size: 13.5px; color: var(--text-secondary); margin-bottom: 28px; }}
  .meta b {{ color: var(--text-primary); font-weight: 600; }}
  .pulse {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 16px;
    padding: 20px 22px 18px; margin-bottom: 32px; }}
  .pulse-top {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }}
  .pulse-label {{ font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--text-tertiary); font-weight: 600; }}
  .pulse-reading {{ font-family: 'IBM Plex Mono', monospace; font-size: 13px; color: var(--accent-warm); }}
  .pulse-track {{ position: relative; height: 8px; border-radius: 5px;
    background: linear-gradient(90deg, #2c5a4a 0%, #3a7a5c 18%, #4d8f5c 32%, #7a8a4a 48%,
      #b8863f 65%, #c65a4a 82%, #d13c4a 100%); margin-bottom: 8px; }}
  .pulse-marker {{ position: absolute; top: -5px; width: 3px; height: 18px; background: #fff;
    border-radius: 2px; box-shadow: 0 0 8px rgba(255,255,255,0.6); }}
  .pulse-scale {{ display: flex; justify-content: space-between; font-size: 10.5px;
    color: var(--text-tertiary); font-family: 'IBM Plex Mono', monospace; }}
  .pulse-note {{ margin-top: 14px; font-size: 13px; color: var(--text-secondary); }}
  .part-label {{ display: flex; align-items: center; gap: 10px; margin: 40px 0 16px; }}
  .part-label .line {{ flex: 1; height: 1px; background: var(--border); min-width: 20px; }}
  .part-label span {{ font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; letter-spacing: 0.1em;
    text-transform: uppercase; color: var(--text-tertiary); white-space: nowrap; }}
  .issue {{ position: relative; padding: 4px 0 4px 18px; margin-bottom: 22px; border-left: 2px solid var(--accent-cool); }}
  .issue:last-child {{ margin-bottom: 0; }}
  .issue-text {{ font-size: 15px; color: var(--text-primary); }}
  .issue-insight {{ display: flex; gap: 8px; margin-top: 10px; padding: 10px 12px;
    background: var(--insight-bg); border: 1px solid var(--insight-border); border-radius: 10px;
    font-size: 13.5px; color: var(--text-secondary); }}
  .issue-insight .icon {{ flex-shrink: 0; }}
  .issue-insight .txt {{ color: var(--insight-text); }}
  .strategy {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 16px;
    padding: 20px 22px; margin-bottom: 14px; }}
  .strategy.highlight {{ border-color: var(--highlight-border);
    background: linear-gradient(180deg, var(--highlight-bg), var(--bg-card) 60%); }}
  .strategy-title {{ font-family: 'Fraunces', serif; font-weight: 600; font-size: 17px;
    margin-bottom: 10px; color: var(--text-primary); }}
  .strategy-title.tonight {{ color: var(--accent-warm); }}
  .strategy.highlight .strategy-title {{ color: var(--accent-cool); }}
  .strategy p {{ margin: 0 0 8px; font-size: 14px; color: var(--text-secondary); }}
  .strategy p:last-child {{ margin-bottom: 0; }}
  .num-pos {{ color: #33c98a; font-family: 'IBM Plex Mono', monospace; font-weight: 600; }}
  .num-neg {{ color: #ff6b7f; font-family: 'IBM Plex Mono', monospace; font-weight: 600; }}
  .num-neu {{ color: var(--accent-cool); font-family: 'IBM Plex Mono', monospace; font-weight: 600; }}
  .footer {{ margin-top: 36px; padding-top: 18px; border-top: 1px solid var(--border);
    font-size: 12px; color: var(--text-tertiary); font-family: 'IBM Plex Mono', monospace; }}
  @media (max-width: 420px) {{ h1 {{ font-size: 25px; }} .wrap {{ padding: 24px 16px 0; }} }}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">DAILY MARKET DIGEST</div>
  <h1>오늘의 정리</h1>
  <div class="meta">{html.escape(time_range)} · 수집 <b>{count}건</b></div>
{pulse_html}
  <div class="part-label"><div class="line"></div><span>Part 1 · 오늘의 핵심 이슈</span><div class="line"></div></div>
  {"".join(issue_html_parts)}

  <div class="part-label"><div class="line"></div><span>Part 2 · 시장 구조 분석</span><div class="line"></div></div>
  {"".join(strategy_html_parts)}
  <div class="footer">TELEGRAM STOCK DEDUP BOT · AUTO-GENERATED SUMMARY</div>
</div>
</body>
</html>"""


# 텔레그램 Bot API 호출 중 순간적인 ConnectionResetError(peer의 TCP 리셋) 등은 30초 주기
# 재전송 큐까지 안 가고 이 자리에서 바로 몇 번 재시도하면 대부분 즉시 복구됨.
_telegram_bot_api_session = requests.Session()
_telegram_bot_api_session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, connect=3, read=2, backoff_factor=0.3,
    allowed_methods=frozenset(["POST"]),
)))


def send_bot_message_raw(chat_id, html_text: str) -> Optional[int]:
    """이미 HTML 서식이 적용된 텍스트를 그대로 전송 (send_bot_notification과 달리
    자동으로 <blockquote>로 감싸지 않음 — 제목은 일반 텍스트로, 본문만 <pre>로 감싸는 등
    직접 구조를 짜고 싶을 때 사용). 성공 시 메시지 id, 실패 시 None."""
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        resp = _telegram_bot_api_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()["result"]["message_id"]
        log.warning(f"봇 메시지 전송 실패: {resp.status_code} {resp.text}")
    except Exception as e:
        log.warning(f"봇 메시지 전송 중 오류: {e}")
    return None


def send_html_document(chat_id, html_content: str, filename: str, caption: str = "") -> Optional[int]:
    """직접 생성한 HTML 콘텐츠(파일로 다운로드해서 온 게 아니라 코드에서 만든 것)를 텔레그램에
    문서로 첨부해서 전송. 텔레그램 클라이언트에서 탭하면 바로 인앱 브라우저로 열어볼 수 있음."""
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        resp = _telegram_bot_api_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"} if caption else {"chat_id": chat_id},
            files={"document": (filename, html_content.encode("utf-8"), "text/html")},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()["result"]["message_id"]
        log.warning(f"HTML 문서 전송 실패: {resp.status_code} {resp.text}")
    except Exception as e:
        log.warning(f"HTML 문서 전송 중 오류: {e}")
    return None


def send_bot_notification(chat_id, text: str, reply_to_message_id: Optional[int] = None) -> Optional[int]:
    """봇 명의로 메시지를 보내 텔레그램 알림이 정상적으로 오도록 함.
    (본인 계정이 보낸 메시지는 텔레그램이 알림을 주지 않기 때문)
    텔레그램은 자유로운 글자색은 지원하지 않지만, 인용구(blockquote) 서식을 쓰면
    왼쪽 색깔 세로줄 + 배경색이 들어가서 일반 메시지와 시각적으로 구분됨.
    성공 시 생성된 메시지 id, 실패 시 None 반환."""
    if not TELEGRAM_BOT_TOKEN:
        return None

    def _post(payload):
        return _telegram_bot_api_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=15,
        )

    styled_text = f"<blockquote>{html.escape(text)}</blockquote>"
    payload = {
        "chat_id": chat_id,
        "text": styled_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    resp = _post(payload)
    if resp.status_code == 200:
        return resp.json()["result"]["message_id"]

    log.warning(f"봇 알림 전송 실패 (1차): {resp.status_code} {resp.text}")

    # reply_to_message_id가 원인일 수 있으니, 답장 없이 한 번 더 시도
    if reply_to_message_id:
        payload.pop("reply_to_message_id", None)
        resp2 = _post(payload)
        if resp2.status_code == 200:
            log.info("답장 없이 재시도해서 알림 전송 성공")
            return resp2.json()["result"]["message_id"]
        log.warning(f"봇 알림 전송 실패 (2차, 답장 없이): {resp2.status_code} {resp2.text}")

    return None



_background_tasks: set = set()


def _spawn(coro) -> asyncio.Task:
    """asyncio.create_task()의 반환값을 아무도 참조하지 않으면 이벤트 루프가 약한 참조만
    들고 있어서 가비지 컬렉터가 실행 도중인 Task를 예고 없이 회수할 수 있음(공식 문서에
    명시된 함정). 무한 루프 형태인 백그라운드 태스크(정리 스케줄러, 재연결 등)가 이래서
    조용히 죽는 걸 막기 위해 모듈 전역 set에 강한 참조를 계속 들고 있음."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def main():
    # 시작 시점이라 아직 동시 접근이 없어 락 없이 to_thread만으로 충분함
    await asyncio.to_thread(load_recent_from_db)

    global _log_queue_info, _log_queue_warning
    if LOG_BOT_TOKEN and LOG_CHAT_INFO:
        _log_queue_info = asyncio.Queue()
        _spawn(_log_shipper_loop(lambda: _log_queue_info, LOG_BOT_TOKEN, LOG_CHAT_INFO))
        log.info(f"INFO 로그 전송 활성화됨 → LOG_CHAT_INFO={LOG_CHAT_INFO}")
    if LOG_BOT_TOKEN and LOG_CHAT_WARNING:
        _log_queue_warning = asyncio.Queue()
        _spawn(_log_shipper_loop(lambda: _log_queue_warning, LOG_BOT_TOKEN, LOG_CHAT_WARNING))
        log.info(f"WARNING+ 로그 전송 활성화됨 → LOG_CHAT_WARNING={LOG_CHAT_WARNING}")

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.start(phone=PHONE)
    log.info("텔레그램 로그인 완료")

    # 이 세션을 "오프라인"으로 표시 → 읽음 상태가 다른 기기로 동기화되는 걸 최대한 줄임
    # (완벽한 차단은 아니며, 텔레그램 계정 자체가 읽음 상태를 기기 간 공유하는 구조라
    #  일부 채널에서는 여전히 읽음 처리가 동기화될 수 있음)
    try:
        from telethon.tl.functions.account import UpdateStatusRequest
        await client(UpdateStatusRequest(offline=True))
        log.info("세션을 오프라인 상태로 설정함 (읽음 동기화 최소화 목적)")
    except Exception as e:
        log.warning(f"오프라인 상태 설정 실패 (무시하고 계속 진행): {e}")

    # 숫자 ID로 지정된 채널/그룹을 찾으려면 먼저 전체 대화방 목록을 한 번
    # 불러와서 내부 캐시에 저장해둬야 함 (안 하면 ID로 조회 시 못 찾는 에러 발생)
    log.info("대화방 목록 캐싱 중...")
    await client.get_dialogs(limit=None)

    source_entities = []
    for chat in SOURCE_CHATS:
        entity = await client.get_entity(chat)
        source_entities.append(entity)
        log.info(f"감시 대상 등록: {chat}")

    # 봇 토큰이 있어도 유저봇(Telethon) 계정으로 직접 보내는 경로(대용량 미디어가 봇 API의
    # 20MB 제한(413)에 걸렸을 때 폴백 등)가 필요해서, 모드와 무관하게 항상 엔티티를 확보해둠.
    # 다만 봇 명의 모드에서는 유저봇 계정이 그 채널의 멤버가 아닐 수도 있으니, 조회 실패해도
    # 시작 자체는 막지 않고 폴백 기능만 비활성화(summary_entity=None)한 채로 계속 진행함.
    summary_entity = None
    digest_entity = None
    try:
        summary_entity = await client.get_entity(DIGEST_CHAT)
        digest_entity = await client.get_entity(SUMMARY_CHAT) if SUMMARY_CHAT != DIGEST_CHAT else summary_entity
    except Exception as e:
        if not TELEGRAM_BOT_TOKEN:
            raise
        log.warning(f"유저봇 계정으로 요약/정리 채널 엔티티 조회 실패(413 폴백 비활성화됨): {e}")
    if not TELEGRAM_BOT_TOKEN:
        log.info(f"요약 채널: {DIGEST_CHAT}")
        log.info(f"정리 채널: {SUMMARY_CHAT}")
    else:
        log.info(f"요약 채널: {DIGEST_CHAT} (봇 명의로 전송)")
        log.info(f"정리 채널: {SUMMARY_CHAT} (봇 명의로 전송)")

    processed_message_ids: dict = {}  # (chat_id, message_id) -> 처리 시각. Telethon 재연결 시
    # 같은 이벤트가 다시 전달되는 경우가 있어, 잡담/중복 여부와 무관하게 완전히 같은 메시지는
    # 한 번만 처리. 시각을 같이 저장해서 주기적으로 오래된 항목을 정리함(무한 누적 방지).

    # is_duplicate()가 쓰는 recent_buffer/DB 같은 공유 상태를 여러 메시지가 동시에 건드리면
    # 경쟁 조건이 생길 수 있어, LLM 호출을 asyncio.to_thread로 event loop 밖에서 돌리는 대신
    # 이 락으로 한 번에 한 메시지씩만 처리하도록 함 (event loop 자체는 안 막히므로 텔레그램
    # 이벤트 수신/재연결 등은 계속 정상 처리됨)
    llm_processing_lock = asyncio.Lock()

    ALBUM_WAIT_SECONDS = 2  # 앨범(여러 장) 메시지가 흩어져 들어오는 걸 이만큼 기다렸다가 한 번에 처리
    pending_albums: dict = {}   # grouped_id -> [event, event, ...]
    album_tasks: dict = {}      # grouped_id -> asyncio.Task (디바운스 타이머)

    # 감시 채널 읽음 처리(ReadHistoryRequest)를 메시지마다 즉시 호출하지 않고 모아뒀다가
    # 주기적으로 채널당 한 번씩만 전송 — 메시지가 몰릴 때마다 매번 호출하면 텔레그램 서버의
    # 일시적 내부 오류(RpcCallFailError: "Telegram is having internal issues")에 걸릴 확률이
    # 높아지고, 그때마다 Telethon 내부 재시도 경고 로그가 찍혀 경고 채널에 스팸성 알림이
    # 쌓이는 문제가 있었음(실제로 겪은 사례).
    pending_read_acks: dict = {}   # chat_id -> (chat_entity, 그 채널에서 본 가장 큰 메시지 id)

    async def send_to_summary_chat(body: str, log_label: str = "") -> Optional[int]:
        try:
            if TELEGRAM_BOT_TOKEN:
                msg_id = send_bot_notification(DIGEST_CHAT, body)
            else:
                sent_msg = await client.send_message(summary_entity, body)
                msg_id = sent_msg.id
        except Exception as e:
            log.warning(f"요약 채널 전송 중 예외 발생: {e}")
            msg_id = None

        if msg_id:
            log.info(f"요약 채널에 전송 완료: {log_label!r}...")
            return msg_id

        log.warning(f"요약 채널 전송 실패 → 재전송 큐에 저장: {log_label!r}")
        try:
            async with llm_processing_lock:
                await asyncio.to_thread(queue_failed_send, DIGEST_CHAT, body)
        except Exception as e:
            log.warning(f"재전송 큐 저장 실패(무시하고 계속): {e}")
        return None

    async def _run_digest(start_hour: int, end_hour: int):
        start_utc, end_utc = _kst_bounds_today(start_hour, end_hour)
        async with llm_processing_lock:
            rows, dup_rows = await asyncio.to_thread(fetch_digest_rows, start_utc, end_utc)
        if not rows and not dup_rows:
            log.warning(f"정리({start_hour:02d}~{end_hour:02d}시): 새로 등록된 정보가 없어 스킵")
            # 스킵도 "이번 주기는 정상적으로 처리했다"는 뜻이므로 발송 시각을 기록해야 함.
            # 안 남기면 다음 재시작 때 catch-up이 이걸 "놓친 정리"로 착각해서 불필요하게 재실행함.
            now_kst_skipped = datetime.now(timezone.utc) + timedelta(hours=9)
            await asyncio.to_thread(set_digest_sent_time, end_hour, now_kst_skipped.isoformat())
            return

        # 요약본이 저장되어 있으면(대부분의 경우) 그걸 쓰고, 없으면(요약 기능이 꺼져있었거나
        # 저장 전에 생긴 옛날 항목) 원문으로 폴백 — 요약본이 훨씬 짧아서 한도 안에 더 많이 담김
        entry_lines = [f"[{label or '출처 미상'}] {summary_text or text}" for text, label, summary_text in rows]
        # 중복으로 판정됐던 것들도 안전망으로 같이 포함 (오탐이었을 수 있으니, 하루 전체를 보는
        # 이 LLM이 최종적으로 다시 한번 진짜 중복인지 판단해서 자연스럽게 걸러내도록 함)
        entry_lines += [f"[{label or '출처 미상'}] {text}" for text, label in dup_rows]

        entries_text = "\n\n".join(entry_lines)
        entries_text = entries_text[:40000]  # 프롬프트 길이 안전장치 (요약본 위주라 넉넉하게 잡음)

        # end_hour(=이 정리를 보내는 트리거 시각)가 정오 이전이면 '오전'(8시 발송, 어젯밤
        # 미국장이 방금 끝나고 한국장이 곧 열리는 시점), 그 외면 '오후'(18시 발송, 오늘
        # 한국장이 막 끝나고 미국장이 곧 열리는 시점).
        digest_time = "morning" if end_hour < 12 else "evening"

        # 정량 데이터 조회 (전부 네트워크 호출이라 스레드로 분리, 실패해도 None 반환되어 정성
        # 데이터만으로 정리가 계속 생성됨). 주식/변동성/환율/에너지는 yfinance, 금리(명목/실질/
        # BEI)는 FRED(API 키 불필요)에서 가져오고, 오후 정리에서만 아시아 지수도 추가 조회함.
        quant_data = await asyncio.to_thread(fetch_market_quant_data)
        rates_data = await asyncio.to_thread(fetch_rates_quant_data)
        asia_data = await asyncio.to_thread(fetch_asia_quant_data) if digest_time == "evening" else None

        quant_text_parts = []
        if quant_data:
            quant_text_parts.append(f"[주식·변동성·환율·에너지]\n{format_quant_data_text(quant_data)}")
        if rates_data:
            quant_text_parts.append(f"[금리 — FRED, 기준일 명시됨]\n{format_rates_data_text(rates_data)}")
        if asia_data:
            quant_text_parts.append(f"[아시아 증시]\n{format_quant_data_text(asia_data)}")
        quant_data_text = "\n\n".join(quant_text_parts) if quant_text_parts else None
        if quant_data_text:
            log.info("정량 데이터 조회 완료, 정리에 포함함")
        else:
            log.warning("정량 데이터 조회 실패/누락 → 텔레그램 텍스트만으로 정리 생성")

        digest = await asyncio.to_thread(
            generate_daily_digest, entries_text, len(entry_lines), quant_data_text, digest_time,
        )
        if not digest:
            log.warning("정리 생성 실패")
            return

        # 혹시 LLM이 지시를 무시하고 마크다운 굵게(**)를 썼으면 제거 (코드블록 안에서는
        # 어차피 렌더링이 안 되고 별표만 그대로 노출되므로)
        digest_clean = re.sub(r"\*\*(.*?)\*\*", r"\1", digest)

        # 혹시 LLM이 응답 전체를 마크다운 코드펜스(```)로 한 번 더 감쌌으면 제거
        # (우리가 이미 <pre>로 감싸는데 LLM도 감싸면 백틱이 글자 그대로 노출됨)
        digest_clean = digest_clean.strip()
        digest_clean = re.sub(r"^```[a-zA-Z]*\n?", "", digest_clean)
        digest_clean = re.sub(r"\n?```$", "", digest_clean).strip()

        # 혹시 LLM이 지시를 무시하고 '오늘의 인사이트' 같은 라벨을 독립된 줄로 남겼으면
        # 미리 제거 (안 그러면 아래 마지막 문단 병합 로직이 라벨 줄을 별도 블록으로 오인함)
        digest_clean = re.sub(r"^\s*오늘의\s*인사이트\s*:?\s*$", "", digest_clean, flags=re.MULTILINE)
        # 마찬가지로 '핵심 이슈 1: 제목' 같은 라벨(뒤에 제목이 붙어있어도)도 지시를 무시하고
        # 남기는 경우가 있어 줄 전체를 제거 (바로 이어지는 '-' 불릿에 이미 같은 내용이
        # 더 상세히 담겨 있어서 라벨 줄 자체가 불필요하고, 남아있으면 아래 블록 재구성 로직을
        # 방해함)
        digest_clean = re.sub(r"^\s*핵심\s*이슈\s*\d+\s*[:：]?.*\n?", "", digest_clean, flags=re.MULTILINE)
        # LLM이 지시를 무시하고 'PART 2' 같은 제목 앞에 마크다운 헤더 기호(#, ##)를 붙이는
        # 경우가 있어 제거 (안 그러면 'PART 1'/'PART 2' 제목 인식 로직이 "## PART 2"를
        # 인식 못 해서 굵게 처리가 안 됨)
        digest_clean = re.sub(r"^\s*#{1,6}\s*", "", digest_clean, flags=re.MULTILINE)
        # LLM이 프롬프트 안의 구조화용 '==== 제목 ====' 표시를 그대로 따라 써서 실제 출력에
        # 새어 나오는 경우가 있어, 앞뒤 '=' 기호들을 제거 (제목 텍스트 자체는 유지)
        digest_clean = re.sub(r"^={2,}\s*|\s*={2,}$", "", digest_clean, flags=re.MULTILINE)
        digest_clean = re.sub(r"\n{3,}", "\n\n", digest_clean).strip()

        # 이슈별 인사이트(💡로 시작하는 줄) 앞에 공백 4칸 들여쓰기를 강제로 보정
        # (LLM이 들여쓰기를 빼먹거나 다르게 넣어도 항상 동일하게 맞춰줌)
        digest_clean = re.sub(r"^[ \t]*💡", "    💡", digest_clean, flags=re.MULTILINE)

        # 이슈 블록들 사이의 빈 줄 삽입을 LLM에게만 맡기면, 이번처럼 LLM이 빈 줄을 빼먹어서
        # 서로 다른 이슈가 한 문단처럼 붙어버리는 문제가 생김. 그래서 '- '로 시작하는 줄이나
        # PART 2의 고정 섹션 헤더(🌡️/🔀/🌐/🌙)를 새 블록의 시작으로 보고, 코드에서 직접
        # 블록을 나눠 빈 줄 1개로 재조립함 (LLM의 빈 줄 삽입 여부와 무관하게 항상 동일한 간격
        # 보장). PART 2 섹션들은 '-' 불릿이 아니라 일반 문단으로 이어지므로, 이 고정 헤더
        # 문자열을 별도로 인식해야 서로 다른 섹션이 한 덩어리로 뭉치는 걸 막을 수 있음.
        DIGEST_SECTION_HEADERS = ("PART 1", "PART 2", "🌡️", "⚡", "🔗", "🔀", "🌐", "🌾", "➡️")
        lines = digest_clean.split("\n")
        rebuilt_blocks = []
        current_block: list = []
        started_bullet_block = False  # 현재 블록이 이미 '-' 불릿으로 시작된 상태인지
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            is_bullet = stripped.startswith("- ")
            is_insight = "💡" in stripped[:6]
            is_section_header = stripped.startswith(DIGEST_SECTION_HEADERS)
            starts_new_block = False
            if (is_bullet or is_section_header) and current_block:
                starts_new_block = True
            elif not is_bullet and not is_insight and started_bullet_block and current_block:
                # 이미 '-' 불릿으로 시작된 이슈 블록 뒤에, '-'도 '💡'도 아닌 일반 문단이 나오면
                # (인사이트가 있든 없든) 새 블록으로 분리
                starts_new_block = True

            if starts_new_block:
                rebuilt_blocks.append("\n".join(current_block).strip())
                current_block = [line]
                started_bullet_block = is_bullet
            else:
                current_block.append(line)
                if is_bullet:
                    started_bullet_block = True
        if current_block:
            rebuilt_blocks.append("\n".join(current_block).strip())
        rebuilt_blocks = [b for b in rebuilt_blocks if b]

        # (오후 정리 전용) '🌙 오늘 밤 미장 핵심 관전 포인트' 섹션을 화살표(➡️)로 시작하도록
        # 강제 보정. 실제 렌더링(텍스트 굵게 처리, HTML)에 쓰이는 rebuilt_blocks 리스트를
        # '직접' 수정해야 함 — 예전엔 별도로 재분할한 문자열만 고쳐서, 정작
        # classify_digest_blocks가 보는 rebuilt_blocks에는 반영이 안 되는 버그가 있었음
        # (🌙 섹션에 화살표가 안 붙던 원인). 오전 정리에는 이 섹션 자체가 없으므로 건너뜀 —
        # 안 그러면 오전 정리의 마지막 섹션(🌐 글로벌 로테이션)에 잘못 화살표가 붙는다.
        if digest_time == "evening":
            tonight_idx = next((i for i, b in enumerate(rebuilt_blocks) if b.startswith("🌙 오늘 밤")), None)
            if tonight_idx is None and rebuilt_blocks:
                tonight_idx = len(rebuilt_blocks) - 1  # 못 찾으면 예전처럼 마지막 블록
            if tonight_idx is not None and not rebuilt_blocks[tonight_idx].startswith("➡️"):
                rebuilt_blocks[tonight_idx] = "➡️ " + rebuilt_blocks[tonight_idx]
        digest_clean = "\n\n".join(rebuilt_blocks) if rebuilt_blocks else digest_clean

        is_overnight = start_hour >= end_hour
        time_range = f"{start_hour:02d}:00~다음날 {end_hour:02d}:00" if is_overnight else f"{start_hour:02d}:00~{end_hour:02d}:00"
        header = f"🗓 정리 ({time_range}, 총 {len(entry_lines)}건)"

        # 텍스트(굵은 제목)와 HTML 파일 둘 다, 같은 블록 구조에서 함께 만듦
        classified_blocks = classify_digest_blocks(rebuilt_blocks) if rebuilt_blocks else []

        sent_msg_id = None
        if TELEGRAM_BOT_TOKEN:
            if classified_blocks:
                digest_body_html = render_digest_text_bold(classified_blocks)
            else:
                digest_body_html = html.escape(digest_clean)
            html_text = f"{html.escape(header)}\n\n<blockquote>{digest_body_html}</blockquote>"
            sent_msg_id = send_bot_message_raw(SUMMARY_CHAT, html_text)
            if sent_msg_id:
                log.info("정리(텍스트) 전송 완료")
                # 사용자에게 실제로 전달되는 핵심 순간은 텍스트 요약 전송 시점이므로, 그 직후
                # 바로 "보냈다"는 기록을 저장함. 이 뒤에 이어지는 HTML 파일 생성/전송 중에
                # 크래시가 나더라도, 이미 텍스트 요약은 받으신 상태이므로 재시작 시 catch-up이
                # "안 보냈다"고 착각해서 중복 발송하는 일이 없도록 함. DB가 아닌 별도 파일에
                # 저장(DB 손상/초기화와 무관하게 이 기록이 안전하게 남도록).
                now_kst_sent = datetime.now(timezone.utc) + timedelta(hours=9)
                await asyncio.to_thread(set_digest_sent_time, end_hour, now_kst_sent.isoformat())

            if classified_blocks:
                vix_info = (quant_data or {}).get("VIX 공포지수")
                digest_html_doc = render_digest_html(
                    classified_blocks, time_range, len(entry_lines),
                    vix_value=vix_info["value"] if vix_info else None,
                    vix_change_pct=vix_info["change_pct"] if vix_info else None,
                )
                now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
                # end_hour(=이 정리를 보내는 트리거 시각)가 정오 이전이면 '오전'(예: 08시 발송,
                # 야간치 정리), 이후면 '오후'(예: 18시 발송, 주간치 정리)로 구분
                ampm_label = "오전" if end_hour < 12 else "오후"
                html_filename = f"{ampm_label}_정리_{now_kst.strftime('%Y%m%d')}.html"
                html_msg_id = send_html_document(SUMMARY_CHAT, digest_html_doc, html_filename)
                if html_msg_id:
                    log.info("정리(HTML 파일) 전송 완료")
                else:
                    log.warning("정리(HTML 파일) 전송 실패")
        else:
            sent_msg = await client.send_message(digest_entity, f"{header}\n\n{digest_clean}")
            sent_msg_id = sent_msg.id
            log.info("정리 전송 완료(유저봇)")
            now_kst_sent = datetime.now(timezone.utc) + timedelta(hours=9)
            await asyncio.to_thread(set_digest_sent_time, end_hour, now_kst_sent.isoformat())

    async def _digest_loop(trigger_hour: int, start_hour: int, end_hour: int):
        def _now_kst():
            return datetime.now(timezone.utc) + timedelta(hours=9)

        # 재시작 시 catch-up: 서버가 꺼져있던 사이(크래시 등) 예정 시각을 놓쳤는지 확인해서,
        # 놓쳤으면 즉시 한 번 재발송하고 정상 스케줄로 넘어감. 놓친 시각으로부터 5분의 여유를
        # 둬서, 마침 트리거 시각 직전에 재시작된 경우 정상 스케줄과 겹쳐 중복 발송되는 걸 방지.
        now_kst = _now_kst()
        most_recent_trigger_kst = now_kst.replace(hour=trigger_hour, minute=0, second=0, microsecond=0)
        if now_kst < most_recent_trigger_kst:
            most_recent_trigger_kst -= timedelta(days=1)
        last_sent_str = await asyncio.to_thread(get_digest_sent_time, trigger_hour)
        last_sent = datetime.fromisoformat(last_sent_str) if last_sent_str else None
        if (
            now_kst - most_recent_trigger_kst > timedelta(minutes=5)
            and (last_sent is None or last_sent < most_recent_trigger_kst)
        ):
            log.warning(
                f"[정리 catch-up] {start_hour:02d}~{end_hour:02d}시 정리가 예정 시각"
                f"({most_recent_trigger_kst.strftime('%Y-%m-%d %H:%M')})에 발송되지 않은 것으로 "
                f"확인됨 → 지금 즉시 재발송 시도"
            )
            try:
                await _run_digest(start_hour, end_hour)
            except Exception as e:
                log.error(f"[정리 catch-up] 재발송 시도 중 오류: {e}")

        while True:
            now_kst = _now_kst()
            target_kst = now_kst.replace(hour=trigger_hour, minute=0, second=0, microsecond=0)
            if now_kst >= target_kst:
                target_kst += timedelta(days=1)
            wait_seconds = (target_kst - now_kst).total_seconds()
            log.info(f"다음 정리({start_hour:02d}~{end_hour:02d}시) 예정 시각까지 {wait_seconds / 3600:.1f}시간 대기")
            await asyncio.sleep(max(wait_seconds, 1))
            try:
                await _run_digest(start_hour, end_hour)
            except Exception as e:
                log.error(f"정리 생성/전송 중 오류: {e}")

    async def _periodic_reconnect_loop():
        """Telethon의 알려진 msg_id 동기화 이슈(오래 연결 유지 시 새 메시지 수신이 조용히
        멈추는 현상)를 예방하기 위해, 일정 시간마다 예방 차원으로 연결을 끊었다 다시 맺음."""
        while True:
            await asyncio.sleep(RECONNECT_INTERVAL_HOURS * 3600)
            try:
                log.info(f"{RECONNECT_INTERVAL_HOURS}시간 경과 → 예방 차원의 재연결 수행")
                await client.disconnect()
                await asyncio.sleep(2)
                await client.connect()
                if not await client.is_user_authorized():
                    log.warning("재연결 후 인증 상태 이상 감지, 확인 필요")
                else:
                    log.info("재연결 완료, 정상적으로 인증된 상태")
            except Exception as e:
                log.error(f"주기적 재연결 실패 (다음 주기에 재시도): {e}")

    _spawn(_periodic_reconnect_loop())
    log.info(f"{RECONNECT_INTERVAL_HOURS}시간마다 예방 차원의 재연결 활성화됨")

    async def _retry_send_queue_loop():
        """전송 실패해서 큐에 쌓인 메시지를 30초마다 재시도. 성공하면 큐에서 제거,
        MAX_SEND_QUEUE_ATTEMPTS번 넘게 실패하면 포기하고 제거(무한 누적 방지)."""
        while True:
            await asyncio.sleep(30)
            try:
                # sqlite3 호출은 동기(블로킹)라, event loop 스레드에서 직접 부르면 디스크
                # I/O가 잠깐이라도 멎었을 때(예: 백업 프로그램과의 경합) 이 프로세스 전체가
                # 얼어붙는다(실제로 겪은 장애 — 재시작 전까지 아무 로그도 안 찍히고 멈춰있었음).
                # to_thread로 별도 스레드에 위임하고, 같은 sqlite3 연결 객체를 여러 스레드가
                # 동시에 건드리지 않도록 llm_processing_lock으로 DB 접근을 한 번에 하나씩만 하게 함.
                async with llm_processing_lock:
                    rows = await asyncio.to_thread(get_pending_sends, 20)
            except Exception as e:
                # DB 파일에 대한 일시적 읽기 오류(예: 'file is not a database') 하나로
                # 이 루프 전체(태스크)가 죽어버리면, 프로세스 재시작 전까지 재전송 큐 안전망이
                # 조용히 영구 비활성화된다(실제로 겪은 장애). 이번 사이클만 건너뛰고 계속 돈다.
                log.warning(f"재전송 큐 조회 실패, 다음 주기에 재시도: {e}")
                continue
            if not rows:
                continue
            log.warning(f"재전송 큐 처리 시작: {len(rows)}건 대기 중")
            for row_id, chat_id, text, attempts, kind, source_chat_id, source_message_id in rows:
                msg_id = None
                try:
                    if kind == "media" and source_chat_id and source_message_id:
                        # 원본 메시지를 다시 조회해서 이미지/미디어를 재전송 시도
                        orig_msg = await client.get_messages(int(source_chat_id), ids=int(source_message_id))
                        orig_real_media = orig_msg and orig_msg.media is not None and not isinstance(orig_msg.media, MessageMediaWebPage)
                        if orig_msg and orig_real_media:
                            try:
                                retry_chat = await client.get_entity(int(source_chat_id))
                                retry_label = getattr(retry_chat, "title", None) or str(source_chat_id)
                            except Exception:
                                retry_label = str(source_chat_id)
                            msg_id = await send_media_to_summary_chat(
                                client, orig_msg, text, summary_entity=summary_entity, source_label=retry_label
                            )
                        else:
                            log.warning(f"재전송용 원본 메시지를 찾을 수 없음 (id={row_id}), 포기")
                            try:
                                async with llm_processing_lock:
                                    await asyncio.to_thread(remove_from_send_queue, row_id)
                            except Exception as e:
                                log.warning(f"재전송 큐 제거 실패(무시하고 계속, id={row_id}): {e}")
                            continue
                    else:
                        if TELEGRAM_BOT_TOKEN:
                            msg_id = send_bot_notification(chat_id, text)
                        else:
                            sent_msg = await client.send_message(summary_entity, text)
                            msg_id = sent_msg.id
                except Exception as e:
                    log.warning(f"재전송 큐 처리 중 예외 (id={row_id}): {e}")
                    msg_id = None

                try:
                    if msg_id:
                        async with llm_processing_lock:
                            await asyncio.to_thread(remove_from_send_queue, row_id)
                        log.warning(f"재전송 성공 (id={row_id}, kind={kind})")
                    else:
                        async with llm_processing_lock:
                            await asyncio.to_thread(bump_send_queue_attempts, row_id)
                        if attempts + 1 >= MAX_SEND_QUEUE_ATTEMPTS:
                            log.warning(f"재전송 {MAX_SEND_QUEUE_ATTEMPTS}회 실패 → 포기하고 큐에서 제거 (id={row_id})")
                            async with llm_processing_lock:
                                await asyncio.to_thread(remove_from_send_queue, row_id)
                except Exception as e:
                    log.warning(f"재전송 큐 상태 갱신 실패(무시하고 계속, id={row_id}): {e}")

    _spawn(_retry_send_queue_loop())
    log.info("재전송 큐 처리 루프 활성화됨 (30초마다 확인)")

    async def _read_ack_flush_loop():
        """process_event가 pending_read_acks에 쌓아둔 감시 채널 읽음 처리 요청을, 채널당
        딱 한 번씩(가장 최근 메시지 id 기준) 모아서 전송. 메시지마다 즉시 호출하지 않는
        이유는 위 pending_read_acks 선언부 주석 참고."""
        while True:
            await asyncio.sleep(20)
            if not pending_read_acks:
                continue
            batch = list(pending_read_acks.items())
            pending_read_acks.clear()
            for chat_key, (chat_entity, max_msg_id) in batch:
                try:
                    await client.send_read_acknowledge(chat_entity, max_id=max_msg_id)
                except Exception as e:
                    log.info(f"감시 채널 읽음 처리 실패(무시하고 계속, chat={chat_key}): {e}")

    _spawn(_read_ack_flush_loop())
    log.info("감시 채널 읽음 처리 루프 활성화됨 (20초마다 모아서 채널당 한 번씩 전송)")

    async def _cleanup_processed_ids_loop():
        """processed_message_ids가 며칠씩 실행하는 동안 무한정 쌓이지 않도록,
        PROCESSED_ID_RETENTION_HOURS(기본 2시간)보다 오래된 항목을 주기적으로 정리."""
        while True:
            await asyncio.sleep(3600)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=PROCESSED_ID_RETENTION_HOURS)
            old_keys = [k for k, v in processed_message_ids.items() if v < cutoff]
            for k in old_keys:
                del processed_message_ids[k]
            if old_keys:
                log.info(f"processed_message_ids 정리: {len(old_keys)}건 제거 (현재 {len(processed_message_ids)}건 유지)")

    _spawn(_cleanup_processed_ids_loop())
    log.info(f"processed_message_ids 정리 루프 활성화됨 ({PROCESSED_ID_RETENTION_HOURS}시간 이상 지난 항목 매시간 정리)")

    if DAILY_DIGEST_ENABLED:
        # 1) 06시~18시 구간 정리, 18시에 전송
        _spawn(_digest_loop(DAILY_DIGEST_HOUR, DAILY_DIGEST_START_HOUR, DAILY_DIGEST_HOUR))
        # 2) 18시~다음날 08시(야간) 구간 정리, 08시에 전송
        _spawn(_digest_loop(NIGHT_DIGEST_END_HOUR, DAILY_DIGEST_HOUR, NIGHT_DIGEST_END_HOUR))
        log.info(
            f"정리 기능 활성화됨 → 매일 {DAILY_DIGEST_HOUR:02d}:00에 "
            f"{DAILY_DIGEST_START_HOUR:02d}:00~{DAILY_DIGEST_HOUR:02d}:00 구간, "
            f"매일 {NIGHT_DIGEST_END_HOUR:02d}:00에 {DAILY_DIGEST_HOUR:02d}:00~다음날 "
            f"{NIGHT_DIGEST_END_HOUR:02d}:00 구간을 각각 전송"
        )

    async def process_event(event, album_events: list = None):
        # 감시 채널 메시지를 처리 시작하는 즉시 읽음 처리 대상으로 등록 — 요약이 이미
        # 요약 채널로 전달되니 원본 감시 채널에는 안 읽은 메시지가 계속 쌓이지 않도록 함.
        # 세션 자체는 여전히 "오프라인"으로 유지해 다른 기기로의 읽음 동기화는 최소화하되
        # (바로 위 UpdateStatusRequest 참고), 감시 채널 자체의 읽음 처리는 명시적으로
        # 수행. 실제 ReadHistoryRequest 호출은 여기서 즉시 하지 않고 pending_read_acks에
        # 쌓아뒀다가 _read_ack_flush_loop가 주기적으로 채널당 한 번씩만 보냄(메시지마다
        # 호출하면 텔레그램 서버 일시 오류에 걸릴 확률이 높아지고 그때마다 Telethon 내부
        # 재시도 경고가 경고 채널에 스팸처럼 쌓이던 문제가 있었음). 등록 자체도 실패하면
        # 안 되므로 예외를 절대 전파하지 않음.
        try:
            read_chat = await event.get_chat()
            chat_key = getattr(read_chat, "id", None)
            max_msg_id = max(ev.message.id for ev in album_events) if album_events else event.message.id
            if chat_key is not None:
                prev = pending_read_acks.get(chat_key)
                if prev is None or max_msg_id > prev[1]:
                    pending_read_acks[chat_key] = (read_chat, max_msg_id)
        except Exception as e:
            log.info(f"감시 채널 읽음 처리 대기열 등록 실패(무시하고 계속): {e}")

        # 스티커/투표는 완전히 무시 (잡담 알림조차 보내지 않음)
        if event.message.sticker:
            log.info("스티커 메시지 감지 → 완전히 무시")
            return
        if isinstance(event.message.media, MessageMediaPoll):
            log.info("텔레그램 투표 메시지 감지 → 완전히 무시")
            return

        text = ""
        if album_events:
            # 앨범은 보통 사진 중 하나에만 캡션이 붙어있으므로, 있는 걸 찾아서 씀
            for ev in album_events:
                if ev.raw_text and ev.raw_text.strip():
                    text = ev.raw_text
                    break
        else:
            text = event.raw_text or ""
        is_html_source = False
        html_filename_stem = None

        # 캡션이 없고 HTML 파일이 첨부된 경우(뉴스 다이제스트 대시보드 등), 파일 내용을
        # 텍스트로 추출해서 그 이후로는 일반 텍스트 메시지처럼 처리(잡담판단/중복판단/요약 재사용)
        if not text.strip() and is_html_document(event.message):
            try:
                html_bytes = await client.download_media(event.message, file=bytes)
                html_content = html_bytes.decode("utf-8", errors="replace")
                text = html_to_text(html_content)
                is_html_source = True
                raw_filename = getattr(getattr(event.message, "file", None), "name", None) or ""
                html_filename_stem = os.path.splitext(raw_filename)[0] or None
                log.info(f"HTML 첨부파일에서 텍스트 추출 완료 ({len(text)}자, 파일명: {raw_filename!r})")
            except Exception as e:
                log.warning(f"HTML 첨부파일 텍스트 추출 실패: {e}")

        # 메시지가 링크 하나뿐이면 텔레그램이 자동으로 미리보기 카드(제목/설명)를 만들어주는데,
        # 우리는 그 정보를 안 가져오면 URL 문자열만 보고 요약해야 해서 부실해짐 → 보강해줌
        webpage_preview = extract_webpage_preview(event.message)
        if webpage_preview and (webpage_preview.get("title") or webpage_preview.get("description")):
            extra_lines = []
            if webpage_preview.get("site_name"):
                extra_lines.append(f"사이트: {webpage_preview['site_name']}")
            if webpage_preview.get("title"):
                extra_lines.append(f"제목: {webpage_preview['title']}")
            if webpage_preview.get("description"):
                extra_lines.append(f"설명: {webpage_preview['description']}")
            text = (text + "\n\n[링크 미리보기 정보]\n" + "\n".join(extra_lines)).strip()
            log.info("링크 미리보기 정보로 텍스트 보강함")

        # 어느 케이스든(잡담/중복/새 정보) 다 필요하므로 가장 먼저 원문 링크부터 계산
        try:
            source_chat = await event.get_chat()
            source_label = getattr(source_chat, "title", None) or getattr(source_chat, "username", "") or "출처"
            source_username = getattr(source_chat, "username", None)
            if source_username:
                source_link = f"https://t.me/{source_username}/{event.message.id}"
            else:
                # 비공개 채널: Telethon 엔티티의 .id는 -100 접두어 없는 원시 채널 ID라 그대로 사용 가능
                source_link = f"https://t.me/c/{source_chat.id}/{event.message.id}"
        except Exception as e:
            log.warning(f"원문 링크 생성 실패: {e}")
            source_label = "출처"
            source_link = None

        if CHITCHAT_FILTER_ENABLED and text.strip() and not is_html_source:
            try:
                async with llm_processing_lock:
                    category = await asyncio.to_thread(classify_is_stock_related, text, extract_urls(text))
            except Exception as e:
                log.error(f"잡담 판단 중 오류, 안전하게 통과시킴: {e}")
                category = "STOCK"
            if category != "STOCK":
                label = "💬 잡담(의견)" if category == "OPINION" else "💬 잡담"
                log.info(f"주식과 무관한 것으로 판단({category}): {text[:40]!r}")
                body = label
                if source_link:
                    body += f"\n🔗 원문 보기: {source_link}"
                await send_to_summary_chat(body, log_label=text[:40])
                return

        matched = None
        if not is_html_source:
            try:
                async with llm_processing_lock:
                    matched = await asyncio.to_thread(
                        is_duplicate, text, source_label=source_label, source_link=source_link or ""
                    )
            except Exception as e:
                log.error(f"중복 판단 중 오류, 안전하게 전달함: {e}")
                matched = None

        if matched:
            # 오탐 대비 안전망: 중복 판정된 원문도 가볍게 남겨둠 (실시간 흐름에는 영향 없음)
            async with llm_processing_lock:
                await asyncio.to_thread(log_duplicate, text, source_label, source_link or "", matched["text"])

            # 동일 내용으로 판단됨 → 매칭된 "이전" 항목의 제목(미리보기), 이전 요약본 링크(또는
            # 그 기능 추가 전 항목이면 등록 시각), 원문 링크를 안내
            matched_preview = make_title_preview(matched["text"], 80)
            body = f"🔁 동일내용(제목: {matched_preview})\n"
            matched_summary_link = matched.get("summary_link")
            if matched_summary_link:
                body += f"\n🔗 이전 요약 보기: {matched_summary_link}"
            else:
                # summary_link가 없는 건 이 기능 추가 전에 등록된 항목이라, 대신 등록 시각(KST)을 보여줌
                matched_time_kst = matched["time"] + timedelta(hours=9)
                time_str = matched_time_kst.strftime("%Y-%m-%d %H:%M")
                body += f"\n{time_str}"
            if source_link:
                body += f"\n\n🔗 원문 보기: {source_link}"
            await send_to_summary_chat(body, log_label=text[:40])
            return

        notify_text = None
        meaningful_caption = len(text.strip()) >= 5  # 너무 짧은 캡션은 LLM한테 보내봐야 의미 없음
        if meaningful_caption and is_market_close_snapshot(text):
            # 지수/자산 가격만 나열하는 스냅샷은 이미 숫자 데이터라 요약할 게 없고, 오히려
            # LLM을 거치면 숫자가 왜곡될 위험이 있어서 원문을 그대로(줄바꿈 유지) 사용함
            notify_text = text.strip()
        elif SUMMARIZE_ENABLED and meaningful_caption:
            async with llm_processing_lock:
                if is_html_source:
                    notify_text = await asyncio.to_thread(summarize_html_digest, text)
                elif is_regional_briefing(text):
                    notify_text = await asyncio.to_thread(summarize_regional_briefing, text)
                elif is_political_briefing(text):
                    notify_text = await asyncio.to_thread(summarize_political_briefing, text)
                elif is_qna_briefing(text):
                    notify_text = await asyncio.to_thread(summarize_qna_briefing, text)
                elif is_reddit_analysis(text):
                    notify_text = await asyncio.to_thread(summarize_reddit_analysis, text)
                elif is_market_close_briefing(text):
                    notify_text = await asyncio.to_thread(summarize_market_close_briefing, text)
                elif is_numbered_news_list(text):
                    notify_text = await asyncio.to_thread(summarize_numbered_news_list, text)
                else:
                    result = await asyncio.to_thread(summarize_message, text)
                    if result:
                        notify_text = result["summary"]
                        if result.get("insight"):
                            notify_text += f"\n\n💡 {result['insight']}"
            if not notify_text:
                # 요약 생성이 실패해도(API 오류 등) 메시지가 통째로 드롭되지 않도록 미리보기로 폴백
                log.warning("요약 생성 실패 → 원문 미리보기로 폴백")
                preview = make_preview(text, 80)
                notify_text = f"{preview}{'...' if len(text.strip()) > 80 else ''}"
        elif meaningful_caption:
            # 요약 기능을 안 켰다면 LLM 호출 없이 원문 앞부분만 알림으로 사용 (무료)
            preview = make_preview(text, 80)
            notify_text = f"{preview}{'...' if len(text.strip()) > 80 else ''}"

        # is_html_source면 이미 파일 내용을 텍스트로 뽑아 처리했으니, 원본 HTML 파일 자체를
        # 또 첨부할 필요는 없음. 그 외 사진/미디어는 캡션 유무와 상관없이 항상 실물을 같이 보냄
        # (텍스트 요약만으로는 "무슨 종목/자산인지" 이미지 없이는 알 수 없는 경우가 많기 때문)
        # 주의: event.message.photo는 진짜 첨부 사진뿐 아니라 링크 미리보기(예: X/트위터
        # 링크 공유 시 뜨는 썸네일)의 사진도 같이 반환하므로, has_real_photo()로 실제
        # 첨부 사진인지 명확히 구분해야 링크 미리보기 이미지까지 전송되는 걸 막을 수 있음.
        real_media = event.message.media is not None and not isinstance(event.message.media, MessageMediaWebPage)
        has_media = (not is_html_source) and bool(has_real_photo(event.message) or real_media)

        # 캡션이 없는 이미지는 텍스트로 판단할 근거가 없어서 여태까지 무조건 통과시켰는데,
        # 밈/캐릭터 삽화 같은 순수 잡담 이미지까지 새 정보로 나가는 문제가 있었음.
        # 캡션 없는 단일 사진(앨범 제외)에 한해 이미지 내용을 직접 봐서 판단.
        is_single_photo = has_real_photo(event.message) and not (album_events and len(album_events) > 1)
        if has_media and not notify_text and CHITCHAT_FILTER_ENABLED and is_single_photo:
            try:
                image_bytes = await client.download_media(event.message, file=bytes)
                async with llm_processing_lock:
                    img_stock_related = await asyncio.to_thread(classify_image_is_stock_related, image_bytes)
                if img_stock_related is False:
                    log.info("이미지 내용만으로 주식과 무관한 잡담(밈 등)으로 판단")
                    chitchat_body = "💬 잡담"
                    if source_link:
                        chitchat_body += f"\n🔗 원문 보기: {source_link}"
                    await send_to_summary_chat(chitchat_body, log_label="(캡션 없는 이미지)")
                    return
            except Exception as e:
                log.warning(f"이미지 잡담 판단 중 오류, 안전하게 통과시킴: {e}")

        if not notify_text and not has_media:
            return

        if is_html_source:
            channel_line = f"[{source_label}] 📄 {html_filename_stem}" if html_filename_stem else f"[{source_label}] 📄"
        else:
            channel_line = f"[{source_label}]"

        if has_media:
            # 캡션은 텔레그램 자체 한도(1024자)가 있어서 긴 요약을 통째로 캡션에 넣을 수
            # 없음. 캡션에는 앞부분만 짧게 넣고, 넘치는 나머지 내용은 잘라서 버리지 않고
            # 사진 전송 직후 별도의 텍스트 메시지(필요하면 여러 개로 분할)로 이어서 보냄.
            MEDIA_CAPTION_LEN = 900
            caption_text = notify_text
            overflow_text = None
            if notify_text and len(notify_text) > MEDIA_CAPTION_LEN:
                cut_at = notify_text.rfind("\n\n", 0, MEDIA_CAPTION_LEN)
                if cut_at < MEDIA_CAPTION_LEN * 0.5:
                    cut_at = MEDIA_CAPTION_LEN
                caption_text = notify_text[:cut_at].rstrip()
                overflow_text = notify_text[cut_at:].strip()

            # 넘치는 내용이 있으면, 캡션을 실제로 보내기 전에 몇 조각으로 나뉠지 먼저 계산해서
            # "(1/N)"부터 "(N/N)"까지 캡션 포함 전체 기준으로 번호를 통일함 (예전엔 캡션엔
            # 번호가 아예 없고 이어지는 메시지만 "이어서 1/1"로 나가서 헷갈렸음)
            overflow_chunks = []
            if overflow_text:
                TELEGRAM_SAFE_CHUNK_LEN = 3900
                paragraphs = overflow_text.split("\n\n")
                current = ""
                for p in paragraphs:
                    candidate = f"{current}\n\n{p}" if current else p
                    if len(candidate) > TELEGRAM_SAFE_CHUNK_LEN and current:
                        overflow_chunks.append(current)
                        current = p
                    else:
                        current = candidate
                    while len(current) > TELEGRAM_SAFE_CHUNK_LEN:
                        overflow_chunks.append(current[:TELEGRAM_SAFE_CHUNK_LEN])
                        current = current[TELEGRAM_SAFE_CHUNK_LEN:]
                if current:
                    overflow_chunks.append(current)

            total_parts = 1 + len(overflow_chunks)
            channel_line_1 = f"{channel_line} (1/{total_parts})" if total_parts > 1 else channel_line
            body = f"{channel_line_1}\n{caption_text}" if caption_text else channel_line_1
            if not overflow_chunks and source_link:
                # 뒤에 이어지는 메시지가 없을 때만 원문 링크를 캡션에 바로 붙임
                # (넘치는 내용이 있으면, 링크는 마지막 이어지는 메시지에 붙임)
                body += f"\n\n🔗 원문 보기: {source_link}"

            if album_events and len(album_events) > 1:
                sent_msg_id = await send_album_to_summary_chat(
                    client, album_events, body, summary_entity=summary_entity, source_label=source_label
                )
                media_label = f"앨범({len(album_events)}장)"
            else:
                sent_msg_id = await send_media_to_summary_chat(
                    client, event.message, body, summary_entity=summary_entity, source_label=source_label
                )
                media_label = "이미지/미디어"
            if sent_msg_id:
                log.info(f"{media_label}(+요약)를 요약 채널에 전송 완료: {text[:40]!r}...")

                if overflow_chunks:
                    log.info(f"캡션이 길어 총 {total_parts}개 메시지로 나눠서 전송함")
                    for idx, oc in enumerate(overflow_chunks, 2):  # 캡션이 1번이므로 2번부터
                        follow_body = f"{channel_line} ({idx}/{total_parts})\n{oc}"
                        if idx == total_parts and source_link:
                            follow_body += f"\n\n🔗 원문 보기: {source_link}"
                        await send_to_summary_chat(follow_body, log_label=text[:40])

                # is_html_source든 아니든 이전 요약 링크는 항상 저장해야, 나중에 같은 내용이
                # 또 오면 "이전 요약 보기" 링크가 정상적으로 붙음 (예전엔 HTML 소스는 이 저장을
                # 건너뛰어서, HTML 첨부로 온 메시지가 중복 처리될 때 링크 대신 시각만 표시되는
                # 문제가 있었음)
                entry_hash = exact_hash(normalize(text))
                async with llm_processing_lock:
                    await asyncio.to_thread(
                        update_entry_summary_link,
                        entry_hash, build_summary_link(sent_msg_id), notify_text or "",
                    )
            else:
                # 이미지 전송 자체가 실패해도 최소한 텍스트 요약/알림은 남기고,
                # 이미지는 원본 위치를 기억해뒀다가 재전송 큐에서 나중에 다시 시도함
                # (앨범은 항목이 여러 개라 재조회가 복잡해서, 일단 단일 이미지/미디어에만 적용)
                await send_to_summary_chat(body, log_label=text[:40])
                if not (album_events and len(album_events) > 1):
                    async with llm_processing_lock:
                        await asyncio.to_thread(
                            queue_failed_media_send, DIGEST_CHAT, body, event.chat_id, event.message.id
                        )
                    log.warning("이미지/미디어 전송 실패 → 재전송 큐에 저장")
            return

        # 텍스트 전용 메시지는 자르지 않고, 텔레그램 한도(4096자)를 넘으면 여러 메시지로
        # 나눠서 순서대로 전송함 (섹션이 많은 브리핑 등에서 내용이 잘려나가는 걸 방지)
        body = f"{channel_line}\n{notify_text}" if notify_text else channel_line
        if source_link:
            body += f"\n\n🔗 원문 보기: {source_link}"

        TELEGRAM_SAFE_CHUNK_LEN = 3900
        if len(body) <= TELEGRAM_SAFE_CHUNK_LEN:
            chunks = [body]
        else:
            # notify_text만 문단(빈 줄) 경계에서 나누고, 채널명은 매 조각 앞에,
            # 원문 링크는 마지막 조각 끝에만 붙임
            paragraphs = (notify_text or "").split("\n\n")
            text_chunks = []
            current = ""
            budget = TELEGRAM_SAFE_CHUNK_LEN - len(channel_line) - 20  # 채널명/번호 표시 여유
            for p in paragraphs:
                candidate = f"{current}\n\n{p}" if current else p
                if len(candidate) > budget and current:
                    text_chunks.append(current)
                    current = p
                else:
                    current = candidate
                # 한 문단 자체가 너무 길면 강제로 글자 수 기준 분할
                while len(current) > budget:
                    text_chunks.append(current[:budget])
                    current = current[budget:]
            if current:
                text_chunks.append(current)

            total = len(text_chunks)
            chunks = []
            for i, tc in enumerate(text_chunks, 1):
                part_label = f"{channel_line} ({i}/{total})"
                chunk_body = f"{part_label}\n{tc}"
                if i == total and source_link:
                    chunk_body += f"\n\n🔗 원문 보기: {source_link}"
                chunks.append(chunk_body)
            log.info(f"메시지가 길어 {total}개로 나눠서 전송함: {text[:40]!r}")

        sent_msg_id = None
        for chunk in chunks:
            msg_id = await send_to_summary_chat(chunk, log_label=text[:40])
            if sent_msg_id is None:
                sent_msg_id = msg_id  # 첫 조각의 메시지 id를 "이전 요약 보기" 링크로 사용
        if sent_msg_id:
            # 나중에 같은 내용이 또 오면 "이전 요약 보기" 링크로 쓸 수 있도록 저장해둠
            entry_hash = exact_hash(normalize(text))
            async with llm_processing_lock:
                await asyncio.to_thread(
                    update_entry_summary_link,
                    entry_hash, build_summary_link(sent_msg_id), notify_text or "",
                )

    async def _flush_album(grouped_id):
        try:
            await asyncio.sleep(ALBUM_WAIT_SECONDS)
        except asyncio.CancelledError:
            return  # 그 사이 새 항목이 도착해서 타이머가 다시 예약된 경우
        events_list = pending_albums.pop(grouped_id, [])
        album_tasks.pop(grouped_id, None)
        if not events_list:
            return
        events_list.sort(key=lambda e: e.message.id)
        log.info(f"앨범 {len(events_list)}장 모아서 처리 시작 (grouped_id={grouped_id})")
        await process_event(events_list[0], album_events=events_list)

    @client.on(events.NewMessage(chats=source_entities))
    async def handler(event):
        msg_key = (event.chat_id, event.message.id)
        if msg_key in processed_message_ids:
            log.info(f"이미 처리한 메시지 재전달(재연결 등) → 스킵: {msg_key}")
            return
        processed_message_ids[msg_key] = datetime.now(timezone.utc)
        # 서버 중단 후 재시작 시 자동으로 이어서 처리(catch-up)할 수 있도록, 처리 시도한
        # 메시지의 ID를 채널별로 계속 갱신해둠 (잡담/중복이어도 "훑어봤다"는 사실 자체가 중요).
        # is_duplicate 등 다른 DB 작업(워커 스레드에서 실행됨)과 같은 SQLite 커넥션을 동시에
        # 건드리면 "cannot commit transaction" 에러가 나므로, 동일하게 락+워커스레드로 감쌈
        async with llm_processing_lock:
            await asyncio.to_thread(set_last_processed_id, event.chat_id, event.message.id)

        grouped_id = event.message.grouped_id
        if grouped_id:
            # 앨범(여러 장)의 일부 → 바로 처리하지 않고 모았다가, 한동안 새 항목이 안 오면 한 번에 처리
            pending_albums.setdefault(grouped_id, []).append(event)
            if grouped_id in album_tasks:
                album_tasks[grouped_id].cancel()
            album_tasks[grouped_id] = asyncio.create_task(_flush_album(grouped_id))
            return

        await process_event(event)

    class _FetchedMessageEvent:
        """iter_messages()로 가져온 과거 Message 객체를, process_event가 기대하는
        '이벤트' 형태로 감싸는 얇은 어댑터. 실시간 NewMessage 이벤트와 인터페이스를
        맞춰서 process_event를 그대로 재사용할 수 있게 함."""

        def __init__(self, message):
            self.message = message
            self.raw_text = message.raw_text or ""
            self.chat_id = message.chat_id

        async def get_chat(self):
            return await self.message.get_chat()

    async def _catch_up_missed_messages():
        """서버 중단 등으로 놓쳤을 수 있는 메시지를, 재시작 시 채널별 마지막 처리 지점부터
        지금까지 자동으로 훑어서 처리. 채널을 처음 감시하는 경우(워터마크 없음)는 과거 전체를
        쏟아붓지 않도록 건너뛰고, 최신 메시지 지점만 기록해서 그다음부터 정상 추적함."""
        for entity in source_entities:
            # entity.id(채널 원본 ID, 접두어 없음)와 event.chat_id(-100 접두어 붙은 정규화 ID)는
            # 서로 다른 값이라서, 여기서도 client.get_peer_id()로 event.chat_id와 동일한 형태로
            # 맞춰야 함. 안 맞추면 실시간 핸들러가 갱신하는 워터마크와 다른 키를 보게 되어,
            # catch-up이 매번 이미 처리된 메시지까지 "처음 본다"고 착각해서 재처리하게 됨
            # (실제로 겪은 버그: 재시작마다 이미 보낸 메시지에 대해 "동일내용" 알림이 대량 발생).
            chat_id = await client.get_peer_id(entity)
            async with llm_processing_lock:
                last_id = await asyncio.to_thread(get_last_processed_id, chat_id)
            if last_id is None:
                try:
                    latest = await client.get_messages(entity, limit=1)
                    if latest:
                        async with llm_processing_lock:
                            await asyncio.to_thread(set_last_processed_id, chat_id, latest[0].id)
                        log.info(f"[catch-up] {getattr(entity, 'title', chat_id)}: 첫 감시라 과거 이력은 건너뛰고 현재 지점부터 추적 시작")
                except Exception as e:
                    log.warning(f"[catch-up] {chat_id} 최신 메시지 조회 실패: {e}")
                continue

            try:
                missed = []
                async for msg in client.iter_messages(entity, min_id=last_id, reverse=True, limit=200):
                    missed.append(msg)
            except Exception as e:
                log.warning(f"[catch-up] {getattr(entity, 'title', chat_id)} 메시지 조회 실패: {e}")
                continue

            if not missed:
                continue

            log.warning(f"[catch-up] {getattr(entity, 'title', chat_id)}: 놓친 메시지 {len(missed)}건 발견, 순서대로 처리 시작")

            # 앨범(grouped_id 공유)은 묶어서, 나머지는 개별로 처리
            i = 0
            processed_count = 0
            while i < len(missed):
                msg = missed[i]
                gid = msg.grouped_id
                if gid:
                    group = [msg]
                    j = i + 1
                    while j < len(missed) and missed[j].grouped_id == gid:
                        group.append(missed[j])
                        j += 1
                    events_group = [_FetchedMessageEvent(m) for m in group]
                    try:
                        await process_event(events_group[0], album_events=events_group)
                    except Exception as e:
                        log.error(f"[catch-up] 앨범 처리 중 오류(무시하고 계속): {e}")
                    # 워터마크는 앨범의 '마지막' 메시지 ID까지 넘겨야 함. 첫 장 ID로만
                    # 저장하면, 다음 catch-up이 앨범 이후 메시지들을 전부 "아직 못 봤다"고
                    # 착각해서 이미 정상 처리된 메시지들까지 계속 재처리하는 버그가 있었음
                    # (실제로 겪은 장애: 앨범 뒤에 있던 메시지들이 계속 중복 발송됨).
                    last_processed_msg = group[-1]
                    i = j
                else:
                    try:
                        await process_event(_FetchedMessageEvent(msg))
                    except Exception as e:
                        log.error(f"[catch-up] 메시지 처리 중 오류(무시하고 계속): {e}")
                    last_processed_msg = msg
                    i += 1
                processed_count += 1
                async with llm_processing_lock:
                    await asyncio.to_thread(set_last_processed_id, chat_id, last_processed_msg.id)

            log.warning(f"[catch-up] {getattr(entity, 'title', chat_id)}: {processed_count}건 처리 완료")

    log.info("놓친 메시지 확인(catch-up) 시작...")
    try:
        await _catch_up_missed_messages()
    except Exception as e:
        log.error(f"catch-up 처리 중 예외 발생(무시하고 실시간 감시로 진행): {e}")
    log.info("catch-up 완료, 실시간 감시로 전환")

    log.info("실시간 감시 시작. 종료하려면 Ctrl+C")
    while True:
        await client.run_until_disconnected()
        # _periodic_reconnect_loop가 예방 차원으로 부르는 client.disconnect()도
        # run_until_disconnected()를 그대로 깨워버려서, 여기서 바로 리턴해버리면
        # main()이 끝나며 프로세스 전체가 exit code 0으로 종료되는 문제가 있었음
        # (그 뒤 launchd가 재시작하면서 매 RECONNECT_INTERVAL_HOURS마다 전체 재기동처럼
        # 보였음). 재연결 task가 client.connect()를 마칠 시간을 준 뒤, 실제로
        # 재연결됐으면 계속 감시를 이어가고, 정말 끊긴 상태일 때만 프로세스를 종료한다.
        await asyncio.sleep(3)
        if client.is_connected():
            log.info("run_until_disconnected 복귀 감지 → 재연결된 상태 확인, 감시 계속")
            continue
        log.warning("run_until_disconnected 복귀 후에도 연결 끊김 상태 → 재연결 시도")
        try:
            await client.connect()
        except Exception as e:
            log.error(f"재연결 시도 실패: {e}")
        if client.is_connected():
            continue
        log.error("재연결 실패 → 프로세스 종료(launchd가 재시작 처리)")
        break


if __name__ == "__main__":
    asyncio.run(main())
