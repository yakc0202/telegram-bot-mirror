"""포트폴리오 조회 기록(Cloudflare D1)을 Grafana가 읽는 로컬 SQLite로 복사한다. launchd가 10분마다 실행.
설정은 저장소 밖 ~/.config/telegram-bot/observability/portfolio.env:
  PORTFOLIO_DIR=wrangler.toml이 있는 폴더, D1_NAME=D1 데이터베이스 이름
방문자 접속 네트워크(회사 이름일 수 있음)와 IP가 담기므로 결과 파일은 저장소 밖에 두고 공개 주소로 열지 않는다."""
import json
import os
import sqlite3
import subprocess
from pathlib import Path

HOME = Path.home()
CONF = HOME / ".config/telegram-bot/observability/portfolio.env"
OUT = HOME / ".local/state/telegram-bot/portfolio-views/views.db"
COLS = ["id", "ts", "path", "status", "country", "city", "asn", "org", "referrer", "bot", "ua", "ip"]


def main():
    conf = dict(line.split("=", 1) for line in CONF.read_text().split() if "=" in line)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(OUT)
    db.execute(f"CREATE TABLE IF NOT EXISTS views ({', '.join(c + (' INTEGER PRIMARY KEY' if c == 'id' else '') for c in COLS)})")
    have = {r[1] for r in db.execute("PRAGMA table_info(views)")}
    for c in COLS:
        if c not in have:
            db.execute(f"ALTER TABLE views ADD COLUMN {c}")
    last = db.execute("SELECT coalesce(max(id), 0) FROM views").fetchone()[0]
    sql = f"SELECT {', '.join(COLS)} FROM views WHERE id > {int(last)} ORDER BY id LIMIT 5000"
    out = subprocess.run(
        ["npx", "-y", "wrangler@4", "d1", "execute", conf["D1_NAME"], "--remote", "--json", "--command", sql],
        cwd=conf["PORTFOLIO_DIR"], capture_output=True, text=True, check=True,
    ).stdout
    rows = json.loads(out)[0]["results"]
    db.executemany(f"INSERT OR IGNORE INTO views ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                   [[r.get(c) for c in COLS] for r in rows])
    db.commit()
    print(f"새 조회 기록 {len(rows)}건 (마지막 id {last} 이후)")


if __name__ == "__main__":
    main()
