"""토론방 웹 앱 - Flask + SocketIO
역할: 토론자(진영별 최대 3명, 채팅 가능, 투표 불가) / 관전자(무제한, 댓글+투표 가능)
"""
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, session, redirect, url_for, g, jsonify
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*")

DB_PATH = os.path.join(os.path.dirname(__file__), "debate.db")
MAX_DEBATTERS_PER_SIDE = 3


# ==================== DB 헬퍼 ====================

def get_db():
    if "db" not in g:
        # timeout=5: DB 잠금 시 5초 대기 후 예외 발생 (무한 대기 방지)
        # WAL 모드 사용 안 함 — Render 에페메럴 FS에서 -wal/-shm 파일 손실 방지
        g.db = sqlite3.connect(DB_PATH, timeout=5)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    # 이전 실행의 WAL/SHM 파일이 남아있으면 메인 DB에 merge 후 삭제
    # (Render 에페메럴 FS에서 재시작 시 -wal/-shm이 정상이전되지 않는 문제 방지)
    import glob
    for suffix in ("-wal", "-shm"):
        wal_path = DB_PATH + suffix
        if os.path.exists(wal_path):
            try:
                c = sqlite3.connect(DB_PATH)
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                c.close()
                os.remove(wal_path)
            except Exception:
                pass
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            red_opinion TEXT NOT NULL DEFAULT '',
            blue_opinion TEXT NOT NULL DEFAULT '',
            duration_minutes INTEGER NOT NULL DEFAULT 10,
            created_at TEXT NOT NULL,
            deadline TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            winner TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            content TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'red' CHECK(side IN ('red','blue')),
            role TEXT NOT NULL DEFAULT 'debater' CHECK(role IN ('debater','observer')),
            created_at TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        );
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            vote TEXT NOT NULL CHECK(vote IN ('red','blue')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(room_id, user_id),
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        );
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'debater' CHECK(role IN ('debater','observer')),
            side TEXT CHECK(side IN ('red','blue')),
            joined_at TEXT NOT NULL,
            UNIQUE(room_id, nickname),
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL UNIQUE,
            nickname TEXT NOT NULL,
            created_at TEXT NOT NULL,
            suspended INTEGER NOT NULL DEFAULT 0,
            suspended_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS moderation_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL DEFAULT 'profanity' CHECK(category IN ('profanity','ad_spam','offtopic')),
            role_scope TEXT NOT NULL DEFAULT 'all' CHECK(role_scope IN ('all','debater','observer')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS moderation_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            room_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            violation_type TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('warning','suspension')),
            warning_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            reported_nickname TEXT NOT NULL,
            reporter_nickname TEXT NOT NULL,
            message_content TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved','dismissed')),
            resolved_by TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        );
    """)
    # 기존 DB에 없는 컬럼 자동 추가 (마이그레이션)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "suspended" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN suspended INTEGER NOT NULL DEFAULT 0")
    if "suspended_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN suspended_at TEXT DEFAULT ''")
    conn.commit()
    conn.close()


init_db()


def seed_demo_rooms():
    """데모 토론 방 4개 삽입 (방이 비어 있을 때만)."""
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
    if count > 0:
        conn.close()
        return
    now = datetime.now()
    demos = [
        ("원격근무는 생산성을 높이는가", "재택근무가 집중력을 높이고 출퇴근 시간을 절약한다.", "출근이 소통과 협업에 필수적이며 사기 저하를 막는다.", 10, "open"),
        ("인공지능은 예술을 대체하는가", "AI가 창작의 민주화를 이루고 새로운 표현 수단을 제공한다.", "예술은 인간의 경험과 감정이 핵심이며 AI는 흉내에 불과하다.", 15, "open"),
        ("주 4일 근무제는 도입해야 하는가", "짧은 근무시간이 삶의 질을 높이고 장기적으로 생산성을 유지한다.", "현실적인 업무량에서 4일제는 과부하와 경쟁력 약화를 초래한다.", 10, "open"),
        ("대학 입시에서 논술은 폐지해야 하는가", "논술은 서술 능력보다 배경 지식을 측정하여 형평성을 해친다.", "논술은 사고력과 표현력을 평가하는 유일한 수단이다.", 20, "ended"),
    ]
    for title, red, blue, dur, status in demos:
        created = now - timedelta(days=2)
        # open 데모 방은 24시간 후 마감 — 즉시 close되는 문제 방지
        deadline = now + timedelta(hours=24) if status == "open" else now - timedelta(days=1)
        winner = "red" if status == "ended" else ""
        conn.execute(
            "INSERT INTO rooms (title, description, red_opinion, blue_opinion, duration_minutes, created_at, deadline, status, winner) VALUES (?,?,?,?,?,?,?,?,?)",
            (title, "", red, blue, dur, created.isoformat(), deadline.isoformat(), status, winner),
        )
    # 종료된 방에 데모 메시지+투표 추가
    ended_id = conn.execute("SELECT id FROM rooms WHERE status='ended'").fetchone()
    if ended_id:
        rid = ended_id[0]
        msgs = [
            ("철수", "데이터를 보면 재택근무 시 집중 시간이 평균 2시간 증가합니다.", "red", "debater"),
            ("영희", "하지만 팀 미팅이 줄면서 신규 프로젝트 진행 속도가 30% 떨어졌어요.", "blue", "debater"),
            ("민수", "개인적으로는 하이브리드 방식이 가장 현실적이라고 봅니다.", "red", "observer"),
            ("지우", "출퇴근 시간 절감 효과가 생각보다 크지 않다는 연구도 있습니다.", "blue", "debater"),
        ]
        for nick, content, side, role in msgs:
            conn.execute(
                "INSERT INTO messages (room_id, nickname, content, side, role, created_at) VALUES (?,?,?,?,?,?)",
                (rid, nick, content, side, role, (now - timedelta(hours=3)).isoformat()),
            )
        votes = [("user1", "red"), ("user2", "blue"), ("user3", "red")]
        for uid, v in votes:
            conn.execute(
                "INSERT INTO votes (room_id, user_id, vote, created_at, updated_at) VALUES (?,?,?,?,?)",
                (rid, uid, v, (now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=2)).isoformat()),
            )
    conn.commit()
    conn.close()


seed_demo_rooms()


def seed_moderation_keywords():
    """기본 차단 단어 삽입 (테이블이 비어 있을 때만)."""
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM moderation_keywords").fetchone()[0]
    if count > 0:
        conn.close()
        return
    now = datetime.now().isoformat()
    keywords = [
        # 욕설 (profanity) — all roles
        ("씨발", "profanity", "all"), ("시발", "profanity", "all"),
        ("개새끼", "profanity", "all"), ("새끼", "profanity", "all"),
        ("병신", "profanity", "all"), ("미친놈", "profanity", "all"),
        ("지랄", "profanity", "all"), ("좆같", "profanity", "all"),
        ("fuck", "profanity", "all"), ("shit", "profanity", "all"),
        ("bitch", "profanity", "all"), ("asshole", "profanity", "all"),
        ("멍청이", "profanity", "all"), ("바보", "profanity", "all"),
        ("idiot", "profanity", "all"), ("stupid", "profanity", "all"),
        # 광고·스팸 (ad_spam) — observers only
        ("쿠팡", "ad_spam", "observer"), ("네이버 쇼핑", "ad_spam", "observer"),
        ("당첨", "ad_spam", "observer"), ("무료 체험", "ad_spam", "observer"),
        ("클릭하세요", "ad_spam", "observer"), ("링크", "ad_spam", "observer"),
        ("http://", "ad_spam", "observer"), ("https://", "ad_spam", "observer"),
        ("팔로우", "ad_spam", "observer"), ("채널 구독", "ad_spam", "observer"),
        # 오프토픽 (offtopic) — debaters only
        ("게임 하자", "offtopic", "debater"), ("밥 먹으러 가자", "offtopic", "debater"),
        ("오늘 날씨", "offtopic", "debater"), ("주식 추천", "offtopic", "debater"),
    ]
    for kw, cat, scope in keywords:
        conn.execute(
            "INSERT OR IGNORE INTO moderation_keywords (keyword, category, role_scope, created_at) VALUES (?,?,?,?)",
            (kw, cat, scope, now),
        )
    conn.commit()
    conn.close()


seed_moderation_keywords()


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("nickname"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        phone = request.form.get("phone_number", "").strip()
        if not phone:
            error = "전화번호를 입력해 주세요."
        elif len(phone) < 10:
            error = "올바른 전화번호를 입력해 주세요."
        else:
            db = get_db()
            row = db.execute(
                "SELECT * FROM users WHERE phone_number=?", (phone,)
            ).fetchone()
            if row:
                session["nickname"] = row["nickname"]
                next_url = request.args.get("next")
                return redirect(next_url or url_for("index"))
            error = "가입되지 않은 전화번호입니다."
    return render_template("login.html", error=error, mode="login")


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("nickname"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        phone = request.form.get("phone_number", "").strip()
        nickname = request.form.get("nickname", "").strip()
        if not phone or not nickname:
            error = "전화번호와 닉네임을 모두 입력해 주세요."
        elif len(phone) < 10:
            error = "올바른 전화번호를 입력해 주세요."
        else:
            db = get_db()
            exists = db.execute(
                "SELECT id FROM users WHERE phone_number=?", (phone,)
            ).fetchone()
            if exists:
                error = "이미 가입된 전화번호입니다."
            else:
                db.execute(
                    "INSERT INTO users (phone_number, nickname, created_at) VALUES (?,?,?)",
                    (phone, nickname, datetime.now().isoformat()),
                )
                db.commit()
                session["nickname"] = nickname
                return redirect(url_for("index"))
    return render_template("login.html", error=error, mode="register")


# ==================== 헬퍼 함수 ====================

import logging

def _bg_check_all_deadlines():
    """백그라운드: 모든 open 방의 마감 시간을 체크하고 종료 이벤트를 보냄.
    동시 접속 시 DB 잠금 문제를 줄이기 위해:
    1. 단일 트랜잭션으로 처리
    2. 불필요한 쿼리 최소화
    3. emit을 마지막에 한 번만 호출"""
    conn = sqlite3.connect(DB_PATH, timeout=5)  # 5초 타임아웃 설정
    conn.row_factory = sqlite3.Row
    closed_rooms = []
    try:
        # 모든 open 방을 한 번에 조회
        rows = conn.execute("SELECT * FROM rooms WHERE status='open'").fetchall()
        
        for row in rows:
            deadline = datetime.fromisoformat(row["deadline"])
            if datetime.now() < deadline:
                continue
            
            # 투표 수 조회
            red_count = conn.execute(
                "SELECT COUNT(*) as c FROM votes WHERE room_id=? AND vote='red'", (row["id"],)
            ).fetchone()["c"]
            blue_count = conn.execute(
                "SELECT COUNT(*) as c FROM votes WHERE room_id=? AND vote='blue'", (row["id"],)
            ).fetchone()["c"]
            
            if red_count > blue_count:
                winner = "빨간 진영"
            elif blue_count > red_count:
                winner = "파란 진영"
            else:
                winner = "무승부"
            
            conn.execute("UPDATE rooms SET status='closed', winner=? WHERE id=?", (winner, row["id"]))
            closed_rooms.append({
                "room_id": row["id"],
                "winner": winner,
                "red_count": red_count,
                "blue_count": blue_count,
                "red_name": row["red_opinion"],
                "blue_name": row["blue_opinion"],
            })
        
        # 한 번에 커밋
        if closed_rooms:
            conn.commit()
            with app.app_context():
                for room_info in closed_rooms:
                    socketio.emit("room_closed", room_info)
    
    except Exception:
        logging.exception("_bg_check_all_deadlines 실패")
        conn.rollback()
    finally:
        conn.close()


def _deadline_scheduler():
    """매 10초마다 open 방의 마감을 체크하는 데몬 스레드.
    동시 접속 시 DB 잠금 문제를 줄이기 위해 체크 주기를 늘림."""
    while True:
        try:
            _bg_check_all_deadlines()
        except Exception:
            logging.exception("_deadline_scheduler 실패")
        time.sleep(10)


def check_deadline(room_id):
    """마감 시간 확인 및 방 상태 업데이트"""
    db = get_db()
    row = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not row:
        return None
    if row["status"] == "open":
        try:
            deadline = datetime.fromisoformat(row["deadline"])
        except (ValueError, TypeError):
            # deadline이 NULL/빈 문자열/잘못된 형식이면 마감 처리하지 않음
            d = dict(row)
            if d.get("deadline") is None:
                d["deadline"] = ""
            return d
        if datetime.now() >= deadline:
            red_count = db.execute(
                "SELECT COUNT(*) as c FROM votes WHERE room_id=? AND vote='red'", (room_id,)
            ).fetchone()["c"]
            blue_count = db.execute(
                "SELECT COUNT(*) as c FROM votes WHERE room_id=? AND vote='blue'", (room_id,)
            ).fetchone()["c"]
            if red_count > blue_count:
                winner = "빨간 진영"
            elif blue_count > red_count:
                winner = "파란 진영"
            else:
                winner = "무승부"
            db.execute("UPDATE rooms SET status='closed', winner=? WHERE id=?", (winner, room_id))
            db.commit()
            # app_context 안에서 emit — 이 요청 컨텍스트가 room_closed의 유일한 emit 경로다.
            with app.app_context():
                socketio.emit("room_closed", {
                    "room_id": room_id,
                    "winner": winner,
                    "red_count": red_count,
                    "blue_count": blue_count,
                    "red_name": row["red_opinion"],
                    "blue_name": row["blue_opinion"],
                })
            row = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if row:
        d = dict(row)
        if d.get("deadline") is None:
            d["deadline"] = ""
        return d
    return None


def get_vote_counts(room_id):
    db = get_db()
    red = db.execute("SELECT COUNT(*) as c FROM votes WHERE room_id=? AND vote='red'", (room_id,)).fetchone()["c"]
    blue = db.execute("SELECT COUNT(*) as c FROM votes WHERE room_id=? AND vote='blue'", (room_id,)).fetchone()["c"]
    return {"red": red, "blue": blue}


def get_debatter_count(room_id, side):
    """특정 진영의 현재 토론자 수"""
    db = get_db()
    return db.execute(
        "SELECT COUNT(*) as c FROM participants WHERE room_id=? AND role='debater' AND side=?",
        (room_id, side)
    ).fetchone()["c"]


def promote_next_debatter(room_id, side):
    """자리가 비면 가장 먼저 대기한 사람을 승격"""
    db = get_db()
    candidate = db.execute(
        "SELECT * FROM participants WHERE room_id=? AND role='debater' AND side IS NULL ORDER BY joined_at ASC LIMIT 1",
        (room_id,)
    ).fetchone()
    if candidate:
        count = get_debatter_count(room_id, side)
        if count < MAX_DEBATTERS_PER_SIDE:
            db.execute("UPDATE participants SET side=? WHERE id=?", (side, candidate["id"]))
            db.commit()
            socketio.emit("participant_update", {
                "room_id": room_id,
                "promoted": candidate["nickname"],
                "side": side,
            })
            return True
    return False


def get_participants_info(room_id):
    """방의 참여자 정보 반환"""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM participants WHERE room_id=? ORDER BY joined_at ASC", (room_id,)
    ).fetchall()
    red_debaters = [r["nickname"] for r in rows if r["role"] == "debater" and r["side"] == "red"]
    blue_debaters = [r["nickname"] for r in rows if r["role"] == "debater" and r["side"] == "blue"]
    waiting = [r["nickname"] for r in rows if r["role"] == "debater" and r["side"] is None]
    observers = [r["nickname"] for r in rows if r["role"] == "observer"]
    return {
        "red_debaters": red_debaters,
        "blue_debaters": blue_debaters,
        "waiting": waiting,
        "observers": observers,
        "red_full": len(red_debaters) >= MAX_DEBATTERS_PER_SIDE,
        "blue_full": len(blue_debaters) >= MAX_DEBATTERS_PER_SIDE,
    }


def get_user_role(room_id, nickname):
    """사용자의 역할 조회: 'debater_red', 'debater_blue', 'debater_waiting', 'observer', None"""
    db = get_db()
    row = db.execute(
        "SELECT * FROM participants WHERE room_id=? AND nickname=?", (room_id, nickname)
    ).fetchone()
    if not row:
        return None
    if row["role"] == "observer":
        return "observer"
    if row["role"] == "debater" and row["side"]:
        return f"debater_{row['side']}"
    if row["role"] == "debater" and not row["side"]:
        return "debater_waiting"
    return None


# ==================== 페이지 라우트 ====================

@app.route("/healthz")
def healthz():
    """Render 헬스체크 전용 — DB·템플릿 접근 없음."""
    return {"status": "ok"}, 200


@app.route("/")
def index():
    db = get_db()
    # 마감 체크는 _deadline_scheduler(10초 주기)가 담당 — 여기서는 조회만
    # 메인에는 진행 중인(open) 토론만 노출
    rooms = db.execute("SELECT * FROM rooms WHERE status='open' ORDER BY created_at DESC").fetchall()
    result = []
    for r in rooms:
        r_dict = dict(r)
        counts = get_vote_counts(r_dict["id"])
        r_dict["red_count"] = counts["red"]
        r_dict["blue_count"] = counts["blue"]
        pinfo = get_participants_info(r_dict["id"])
        r_dict["red_debatters"] = pinfo["red_debaters"]
        r_dict["blue_debatters"] = pinfo["blue_debaters"]
        r_dict["observer_count"] = len(pinfo["observers"])
        result.append(r_dict)
    return render_template("index.html", rooms=result)


@app.route("/my-debates")
def my_debates():
    """나의 토론 — 본인이 참여하거나 관전한 모든 토론(진행중+종료)"""
    if not session.get("nickname"):
        return redirect(url_for("login", next=request.path))
    nickname = session["nickname"]
    db = get_db()
    rows = db.execute(
        """SELECT r.*, p.role AS my_role, p.side AS my_side
           FROM rooms r
           JOIN participants p ON p.room_id = r.id AND p.nickname = ?
           ORDER BY r.created_at DESC""",
        (nickname,)
    ).fetchall()
    result = []
    for r in rows:
        r_dict = dict(r)
        counts = get_vote_counts(r_dict["id"])
        r_dict["red_count"] = counts["red"]
        r_dict["blue_count"] = counts["blue"]
        pinfo = get_participants_info(r_dict["id"])
        r_dict["observer_count"] = len(pinfo["observers"])
        r_dict["red_debatters"] = pinfo["red_debaters"]
        r_dict["blue_debatters"] = pinfo["blue_debaters"]
        result.append(r_dict)
    return render_template("my_debates.html", rooms=result, nickname=nickname)


@app.route("/my")
def my_page():
    """마이페이지 — 내 정보 관리 (닉네임·전화번호·가입일)"""
    if not session.get("nickname"):
        return redirect(url_for("login", next=request.path))
    nickname = session["nickname"]
    db = get_db()
    user = db.execute(
        "SELECT phone_number, created_at FROM users WHERE nickname = ?",
        (nickname,)
    ).fetchone()
    return render_template("my.html", nickname=nickname, user=user)


@app.route("/top")
def top_week():
    """이주의 Top10 — 최근 7일 종료 토론 중 조회수·댓글·투표 종합점수 상위 10개"""
    db = get_db()
    since = (datetime.now() - timedelta(days=7)).isoformat()
    rooms = db.execute(
        "SELECT * FROM rooms WHERE status='closed' AND created_at >= ? ORDER BY created_at DESC",
        (since,)
    ).fetchall()
    scored = []
    for r in rooms:
        rid = r["id"]
        views = db.execute(
            "SELECT COUNT(DISTINCT nickname) c FROM participants WHERE room_id=?", (rid,)
        ).fetchone()["c"]
        comments = db.execute(
            "SELECT COUNT(*) c FROM messages WHERE room_id=?", (rid,)
        ).fetchone()["c"]
        votes = get_vote_counts(rid)
        vtotal = votes["red"] + votes["blue"]
        # 종합점수: 조회수 × 3 + 댓글 수 + 총 투표 수
        score = views * 3 + comments + vtotal
        d = dict(r)
        d["view_count"] = views
        d["comment_count"] = comments
        d["red_count"] = votes["red"]
        d["blue_count"] = votes["blue"]
        d["vote_total"] = vtotal
        d["score"] = score
        scored.append(d)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return render_template("top.html", rooms=scored[:10])


@app.route("/join", methods=["GET", "POST"])
def join_page():
    # 토론 생성·진입은 로그인 필요 — 미로그인이라면 로그인으로
    if not session.get("nickname"):
        return redirect(url_for("login", next=request.path))

    error = None
    nickname = session["nickname"]
    if request.method == "POST":
        action = request.form.get("action", "list")
        target_room = request.form.get("target_room", "").strip()

        if action == "create":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            red_opinion = request.form.get("red_opinion", "").strip()
            blue_opinion = request.form.get("blue_opinion", "").strip()
            try:
                duration = int(request.form.get("duration", 10))
            except ValueError:
                duration = 10
            duration = max(1, min(duration, 120))

            if not title:
                error = "토론 주제를 입력해 주세요."
            elif not red_opinion or not blue_opinion:
                error = "두 진영의 의견을 모두 입력해 주세요."
            else:
                now = datetime.now()
                deadline = now + timedelta(minutes=duration)
                db = get_db()
                cur = db.execute(
                    """INSERT INTO rooms (title, description, red_opinion, blue_opinion, duration_minutes, created_at, deadline)
                       VALUES (?,?,?,?,?,?,?)""",
                    (title, description, red_opinion, blue_opinion, duration, now.isoformat(), deadline.isoformat()),
                )
                db.commit()
                new_room_id = cur.lastrowid
                # 생성자는 관전자로 자동 등록
                db.execute(
                    "INSERT OR IGNORE INTO participants (room_id, nickname, role, side, joined_at) VALUES (?,?,?,?,?)",
                    (new_room_id, nickname, "observer", None, now.isoformat())
                )
                db.commit()
                return redirect(url_for("room_detail", room_id=new_room_id))

        elif action == "enter" and target_room:
            role = request.form.get("role", "observer")
            side = request.form.get("side", "")

            db = get_db()
            room_row = db.execute("SELECT * FROM rooms WHERE id=?", (int(target_room),)).fetchone()
            if not room_row:
                return redirect(url_for("index"))
            room = dict(room_row)
            if room["status"] != "open":
                return redirect(url_for("room_detail", room_id=target_room))

            db = get_db()
            now = datetime.now().isoformat()

            if role == "debater":
                if not side or side not in ("red", "blue"):
                    error = "진영을 선택해 주세요."
                else:
                    existing = db.execute(
                        "SELECT * FROM participants WHERE room_id=? AND nickname=?",
                        (room["id"], nickname)
                    ).fetchone()
                    if existing:
                        db.execute(
                            "UPDATE participants SET role='debater', side=?, joined_at=? WHERE id=?",
                            (side, now, existing["id"])
                        )
                    else:
                        count = get_debatter_count(room["id"], side)
                        if count >= MAX_DEBATTERS_PER_SIDE:
                            db.execute(
                                "INSERT INTO participants (room_id, nickname, role, side, joined_at) VALUES (?,?,?,?,?)",
                                (room["id"], nickname, "debater", None, now)
                            )
                        else:
                            db.execute(
                                "INSERT INTO participants (room_id, nickname, role, side, joined_at) VALUES (?,?,?,?,?)",
                                (room["id"], nickname, "debater", side, now)
                            )
                    db.commit()
                    return redirect(url_for("room_detail", room_id=target_room))

            else:  # observer
                existing = db.execute(
                    "SELECT * FROM participants WHERE room_id=? AND nickname=?",
                    (room["id"], nickname)
                ).fetchone()
                if existing:
                    db.execute(
                        "UPDATE participants SET role='observer', side=NULL, joined_at=? WHERE id=?",
                        (now, existing["id"])
                    )
                else:
                    db.execute(
                        "INSERT INTO participants (room_id, nickname, role, side, joined_at) VALUES (?,?,?,?,?)",
                        (room["id"], nickname, "observer", None, now)
                    )
                db.commit()
                return redirect(url_for("room_detail", room_id=target_room))

        else:
            return redirect(url_for("index"))

    db = get_db()
    # 마감 체크는 _deadline_scheduler(10초 주기)가 담당 — 여기서는 조회만
    rooms = db.execute("SELECT * FROM rooms WHERE status='open' ORDER BY created_at DESC").fetchall()
    rooms_list = []
    for r in rooms:
        r_dict = dict(r)
        pinfo = get_participants_info(r_dict["id"])
        r_dict["red_slots_left"] = MAX_DEBATTERS_PER_SIDE - len(pinfo["red_debaters"])
        r_dict["blue_slots_left"] = MAX_DEBATTERS_PER_SIDE - len(pinfo["blue_debaters"])
        rooms_list.append(r_dict)
    return render_template("join.html", error=error, rooms=rooms_list)


@app.route("/room/<int:room_id>")
def room_detail(room_id):
    # 마감 체크는 _deadline_scheduler(10초 주기)가 담당 — 여기서는 조회만
    db = get_db()
    room_row = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not room_row:
        return redirect(url_for("index"))
    room = dict(room_row)

    is_closed = room["status"] == "closed"
    nickname = session.get("nickname")

    # 종료된 토론은 누구나 기록을 볼 수 있음 (로그인 불요).
    # 진행 중인(open) 토론만 닉네임이 필요하며, 이때에만 참여자로 등록된다.
    if not nickname and not is_closed:
        return redirect(url_for("join_page"))

    db = get_db()
    messages = db.execute(
        "SELECT * FROM messages WHERE room_id=? ORDER BY created_at ASC", (room_id,)
    ).fetchall()
    counts = get_vote_counts(room_id)

    my_vote = ""
    my_role = None
    if nickname:
        # 진행 중인 방에 입장하면 관전자로 자동 등록
        if not is_closed:
            existing = db.execute(
                "SELECT id FROM participants WHERE room_id=? AND nickname=?",
                (room_id, nickname)
            ).fetchone()
            if not existing:
                from datetime import datetime
                db.execute(
                    "INSERT INTO participants (room_id, nickname, role, side, joined_at) VALUES (?,?,?,?,?)",
                    (room_id, nickname, "observer", None, datetime.now().isoformat())
                )
                db.commit()
        my_vote_row = db.execute(
            "SELECT vote FROM votes WHERE room_id=? AND user_id=?", (room_id, nickname)
        ).fetchone()
        my_vote = my_vote_row["vote"] if my_vote_row else ""
        my_role = get_user_role(room_id, nickname)

    total = counts["red"] + counts["blue"]
    red_pct = (counts["red"] / total * 100) if total > 0 else 50
    blue_pct = (counts["blue"] / total * 100) if total > 0 else 50

    pinfo = get_participants_info(room_id)

    from_list = request.args.get("source") in ("top", "my")

    return render_template(
        "room.html",
        room=room,
        messages=[dict(m) for m in messages],
        red_count=counts["red"],
        blue_count=counts["blue"],
        red_pct=red_pct,
        blue_pct=blue_pct,
        my_vote=my_vote,
        nickname=nickname or "",
        my_role=my_role,
        participants=pinfo,
        max_per_side=MAX_DEBATTERS_PER_SIDE,
        from_list=from_list,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ==================== REST API ====================

@app.route("/api/vote/<int:room_id>", methods=["POST"])
def api_vote(room_id):
    """투표 - 관전자만 가능"""
    user_id = session.get("nickname", "")
    if not user_id:
        return jsonify({"error": "닉네임이 필요합니다"}), 401

    role = get_user_role(room_id, user_id)
    if role != "observer":
        return jsonify({"error": "토론 참여자는 투표할 수 없습니다. 관전자만 투표 가능합니다."}), 403

    data = request.get_json(force=True)
    vote = data.get("vote", "").strip().lower()
    if vote not in ("red", "blue"):
        return jsonify({"error": "유효하지 않은 투표입니다"}), 400

    db = get_db()
    room_row = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not room_row or room_row["status"] != "open":
        return jsonify({"error": "투표가 마감되었습니다"}), 400

    db = get_db()
    now = datetime.now().isoformat()
    db.execute(
        """INSERT INTO votes (room_id, user_id, vote, created_at, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(room_id, user_id) DO UPDATE SET vote=excluded.vote, updated_at=excluded.updated_at""",
        (room_id, user_id, vote, now, now),
    )
    db.commit()

    counts = get_vote_counts(room_id)
    socketio.emit("vote_update", {
        "room_id": room_id,
        "red": counts["red"],
        "blue": counts["blue"],
        "voter": user_id,
        "vote": vote,
    })
    return jsonify({"ok": True, "red": counts["red"], "blue": counts["blue"]})


@app.route("/api/leave/<int:room_id>", methods=["POST"])
def api_leave(room_id):
    """방 나가기 - 토론자 자리 해제 + 승격"""
    user_id = session.get("nickname", "")
    if not user_id:
        return jsonify({"error": "에러"}), 401

    db = get_db()
    row = db.execute(
        "SELECT * FROM participants WHERE room_id=? AND nickname=?", (room_id, user_id)
    ).fetchone()
    if row and row["role"] == "debater" and row["side"]:
        freed_side = row["side"]
        db.execute("DELETE FROM participants WHERE id=?", (row["id"],))
        db.commit()
        promote_next_debatter(room_id, freed_side)
        socketio.emit("participant_update", {
            "room_id": room_id,
            "left": user_id,
            "side": freed_side,
        })
    elif row:
        db.execute("DELETE FROM participants WHERE id=?", (row["id"],))
        db.commit()

    return jsonify({"ok": True})


@app.route("/api/switch_role/<int:room_id>", methods=["POST"])
def api_switch_role(room_id):
    """역할 전환: 관전자 ↔ 토론자"""
    user_id = session.get("nickname", "")
    if not user_id:
        return jsonify({"error": "에러"}), 401

    data = request.get_json(force=True)
    new_role = data.get("role", "")
    side = data.get("side", "")

    db = get_db()
    now = datetime.now().isoformat()
    row = db.execute(
        "SELECT * FROM participants WHERE room_id=? AND nickname=?", (room_id, user_id)
    ).fetchone()

    if new_role == "debater":
        if not side or side not in ("red", "blue"):
            return jsonify({"error": "진영을 선택해 주세요"}), 400
        count = get_debatter_count(room_id, side)
        if count >= MAX_DEBATTERS_PER_SIDE:
            if row:
                db.execute("UPDATE participants SET role='debater', side=NULL, joined_at=? WHERE id=?", (now, row["id"]))
            else:
                db.execute(
                    "INSERT INTO participants (room_id, nickname, role, side, joined_at) VALUES (?,?,?,?,?)",
                    (room_id, user_id, "debater", None, now)
                )
            db.commit()
            return jsonify({"ok": True, "status": "waiting"})
        else:
            if row:
                db.execute("UPDATE participants SET role='debater', side=?, joined_at=? WHERE id=?", (side, now, row["id"]))
            else:
                db.execute(
                    "INSERT INTO participants (room_id, nickname, role, side, joined_at) VALUES (?,?,?,?,?)",
                    (room_id, user_id, "debater", side, now)
                )
            db.commit()
            return jsonify({"ok": True, "status": "debater", "side": side})

    elif new_role == "observer":
        if row and row["role"] == "debater" and row["side"]:
            freed_side = row["side"]
            db.execute("UPDATE participants SET role='observer', side=NULL, joined_at=? WHERE id=?", (now, row["id"]))
            db.commit()
            promote_next_debatter(room_id, freed_side)
        elif row:
            db.execute("UPDATE participants SET role='observer', side=NULL, joined_at=? WHERE id=?", (now, row["id"]))
            db.commit()
        else:
            db.execute(
                "INSERT INTO participants (room_id, nickname, role, side, joined_at) VALUES (?,?,?,?,?)",
                (room_id, user_id, "observer", None, now)
            )
            db.commit()
        return jsonify({"ok": True, "status": "observer"})

    return jsonify({"error": "잘못된 요청"}), 400


# ==================== 신고 & 관리자 ====================

ADMIN_PASSWORD = "admin123"

@app.route("/api/report/<int:room_id>", methods=["POST"])
def api_report(room_id):
    """메시지 신고"""
    nickname = session.get("nickname", "")
    if not nickname:
        return jsonify({"error": "로그인이 필요합니다"}), 401

    data = request.get_json(force=True)
    target_nickname = data.get("target_nickname", "")
    reason = data.get("reason", "").strip()[:500]

    if not target_nickname:
        return jsonify({"error": "신고 대상이 없습니다"}), 400

    db = get_db()
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO reports (room_id, reported_nickname, reporter_nickname, message_content, reason, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (room_id, target_nickname, nickname, "", reason, "pending", now),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    """관리자 페이지 — 정지 해제·신고 처리·비밀번호 변경"""
    global ADMIN_PASSWORD
    error = None
    success = None

    if request.method == "POST":
        action = request.form.get("action", "")
        pwd = request.form.get("password", "")

        # 1차: 관리자 인증 (session에 없으면 여기서 처리)
        if not session.get("admin_authed"):
            if action == "authenticate":
                if pwd == ADMIN_PASSWORD:
                    session["admin_authed"] = True
                    success = "인증되었습니다"
                else:
                    error = "비밀번호가 올바르지 않습니다"
            else:
                error = "관리자 비밀번호를 입력하세요"
        else:
            # 2차: 실제 액션 처리
            if action == "logout_admin":
                session.pop("admin_authed", None)
                success = "로그아웃되었습니다"
            elif action == "change_password":
                new_pwd = request.form.get("new_password", "")
                confirm_pwd = request.form.get("confirm_pwd", "")
                if pwd != ADMIN_PASSWORD:
                    error = "현재 비밀번호가 올바르지 않습니다"
                elif len(new_pwd) < 4:
                    error = "새 비밀번호는 4자 이상이어야 합니다"
                elif new_pwd != confirm_pwd:
                    error = "새 비밀번호와 확인이 일치하지 않습니다"
                else:
                    ADMIN_PASSWORD = new_pwd
                    success = "관리자 비밀번호를 변경했습니다"
            else:
                db = get_db()
                if action == "unsuspend":
                    nick = request.form.get("nickname", "")
                    db.execute("UPDATE users SET suspended=0, suspended_at=NULL WHERE nickname=?", (nick,))
                    db.commit()
                    socketio.emit("moderation_unsuspended", {"nickname": nick})
                    success = f"'{nick}' 정지를 해제했습니다"
                elif action == "resolve_report":
                    report_id = request.form.get("report_id", "")
                    db.execute("UPDATE reports SET status='resolved' WHERE id=?", (report_id,))
                    db.commit()
                    success = "신고를 처리했습니다"
                elif action == "delete_report":
                    report_id = request.form.get("report_id", "")
                    db.execute("DELETE FROM reports WHERE id=?", (report_id,))
                    db.commit()
                    success = "신고를 삭제했습니다"
                elif action == "add_keyword":
                    kw = request.form.get("keyword", "").strip()
                    cat = request.form.get("category", "profanity")
                    if not kw:
                        error = "단어를 입력하세요"
                    elif cat not in ("profanity", "ad_spam", "offtopic"):
                        error = "잘못된 카테고리입니다"
                    else:
                        try:
                            db.execute(
                                "INSERT INTO moderation_keywords (keyword, category, role_scope, created_at) VALUES (?,?,?,?)",
                                (kw, cat, "all", __import__("datetime").datetime.now().isoformat())
                            )
                            db.commit()
                            success = f"'{kw}' 단어를 추가했습니다"
                        except Exception:
                            error = f"'{kw}' 이미 존재하는 단어입니다"
                elif action == "delete_keyword":
                    kw_id = request.form.get("keyword_id", "")
                    db.execute("DELETE FROM moderation_keywords WHERE id=?", (kw_id,))
                    db.commit()
                    success = "단어를 삭제했습니다"

    # 차단 단어 필터 + 페이지네이션
    kw_category = request.args.get("category", "")
    kw_page = max(1, int(request.args.get("page", 1)))
    kw_per_page = 15

    db = get_db()
    if kw_category:
        kw_total = db.execute(
            "SELECT COUNT(*) FROM moderation_keywords WHERE category=?", (kw_category,)
        ).fetchone()[0]
        kw_offset = (kw_page - 1) * kw_per_page
        all_keywords = db.execute(
            "SELECT * FROM moderation_keywords WHERE category=? ORDER BY keyword LIMIT ? OFFSET ?",
            (kw_category, kw_per_page, kw_offset)
        ).fetchall()
    else:
        kw_total = db.execute("SELECT COUNT(*) FROM moderation_keywords").fetchone()[0]
        kw_offset = (kw_page - 1) * kw_per_page
        all_keywords = db.execute(
            "SELECT * FROM moderation_keywords ORDER BY category, keyword LIMIT ? OFFSET ?",
            (kw_per_page, kw_offset)
        ).fetchall()
    kw_total_pages = max(1, (kw_total + kw_per_page - 1) // kw_per_page)

    suspended_users = db.execute(
        "SELECT nickname, suspended_at FROM users WHERE suspended=1 ORDER BY suspended_at DESC"
    ).fetchall()
    pending_reports = db.execute(
        "SELECT * FROM reports WHERE status='pending' ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    suspensions = db.execute(
        "SELECT * FROM moderation_violations WHERE action='suspension' ORDER BY created_at DESC LIMIT 30"
    ).fetchall()

    return render_template("admin.html",
                           suspended_users=suspended_users,
                           pending_reports=pending_reports,
                           all_keywords=all_keywords,
                           kw_total=kw_total,
                           kw_page=kw_page,
                           kw_total_pages=kw_total_pages,
                           kw_category=kw_category,
                           kw_per_page=kw_per_page,
                           suspensions=suspensions,
                           error=error,
                           success=success)


# ==================== Socket.IO ====================

# ── 비매너 방지 정책 ──────────────────────────────────────────────
import re as _re

def _normalize_text(text):
    """변형 문자 처리: 분절·유사 문자 제거 후 소문자화."""
    t = text.lower()
    # 영문 대소문자 통일 + 특수 기호 제거
    t = _re.sub(r"[^\w\s]", "", t)
    # 한글 분절 자모 합치기 (간단한 매핑)
    jamo_map = {
        "ㄱ":"ㄱ","ㄲ":"ㄲ","ㄴ":"ㄴ","ㄷ":"ㄷ","ㄸ":"ㄸ","ㄹ":"ㄹ","ㅁ":"ㅁ",
        "ㅂ":"ㅂ","ㅃ":"ㅃ","ㅅ":"ㅅ","ㅆ":"ㅆ","ㅇ":"ㅇ","ㅈ":"ㅈ","ㅉ":"ㅉ",
        "ㅊ":"ㅊ","ㅋ":"ㅋ","ㅌ":"ㅌ","ㅍ":"ㅍ","ㅎ":"ㅎ",
        "ㅏ":"ㅏ","ㅐ":"ㅐ","ㅑ":"ㅑ","ㅒ":"ㅒ","ㅓ":"ㅓ","ㅔ":"ㅔ","ㅕ":"ㅕ","ㅖ":"ㅖ",
        "ㅗ":"ㅗ","ㅘ":"ㅘ","ㅙ":"ㅙ","ㅚ":"ㅚ","ㅛ":"ㅛ","ㅜ":"ㅜ","ㅝ":"ㅝ","ㅞ":"ㅞ","ㅟ":"ㅟ","ㅠ":"ㅠ",
        "ㅡ":"ㅡ","ㅢ":"ㅢ","ㅣ":"ㅣ",
    }
    # 분절된 자모를 합쳐 원본 글자로 복원
    result = []
    i = 0
    while i < len(t):
        ch = t[i]
        if ch in jamo_map and i + 2 < len(t) and t[i+1] in jamo_map and t[i+2] in jamo_map:
            # 초성+중성+종성 조합 → 한글 자모 조합은 간단히 그대로 유지
            pass
        result.append(ch)
        i += 1
    return "".join(result)

def check_moderation(db, room_id, nickname, content, role):
    """
    메시지 전송 전 비매너 검사.
    반환: (blocked: bool, action: str|None, warning_count: int, violation_type: str|None)
      - blocked=True  → 전송 불가
      - action='warning'     → 경고 1회 차감
      - action='suspension'  → 계정 정지
    """
    normalized = _normalize_text(content)

    # 1) 계정 정지 여부 확인
    user_row = db.execute(
        "SELECT suspended FROM users WHERE nickname=?", (nickname,)
    ).fetchone()
    if user_row and user_row["suspended"]:
        return True, "suspended", 99, None

    # 2) 키워드 매칭 — 역할별 범위 적용
    scope = "all"
    if role == "debater":
        scope = "debater"
    elif role == "observer":
        scope = "observer"

    # 해당 역할에 적용되는 카테고리
    if role == "debater":
        categories = ("profanity", "ad_spam", "offtopic")
    else:
        categories = ("profanity", "ad_spam")

    rows = db.execute(
        "SELECT keyword, category FROM moderation_keywords WHERE role_scope IN ('all', ?)",
        (scope,),
    ).fetchall()

    matched_keyword = None
    matched_category = None
    for row in rows:
        if row["category"] not in categories:
            continue
        kw_normalized = _normalize_text(row["keyword"])
        if kw_normalized in normalized:
            matched_keyword = row["keyword"]
            matched_category = row["category"]
            break

    # 3) 반복 메시지 감지 (토론자 전용) — 최근 5개 메시지와 동일하면 위반
    if role == "debater" and not matched_keyword:
        recent = db.execute(
            "SELECT content FROM messages WHERE room_id=? AND nickname=? ORDER BY id DESC LIMIT 5",
            (room_id, nickname),
        ).fetchall()
        recent_norms = [_normalize_text(r["content"]) for r in recent]
        if len(recent_norms) >= 2 and recent_norms.count(normalized) >= 2:
            matched_keyword = "(반복 메시지)"
            matched_category = "repeat"

    # 4) 위반 없으면 통과
    if not matched_keyword:
        return False, None, 0, None

    # 5) 누적 경고 수 계산 (계정 전체)
    prev_count = db.execute(
        "SELECT COUNT(*) as cnt FROM moderation_violations WHERE nickname=? AND action='warning'",
        (nickname,),
    ).fetchone()["cnt"]

    new_count = prev_count + 1
    now = datetime.now().isoformat()

    if new_count >= 3:
        # 3회차 → 계정 정지
        db.execute(
            "UPDATE users SET suspended=1, suspended_at=? WHERE nickname=?",
            (now, nickname),
        )
        db.execute(
            "INSERT INTO moderation_violations (nickname, room_id, content, violation_type, action, warning_count, created_at) VALUES (?,?,?,?,?,?,?)",
            (nickname, room_id, content, matched_category, "suspension", new_count, now),
        )
        db.commit()
        return True, "suspension", new_count, matched_category
    else:
        # 1~2회차 → 경고
        db.execute(
            "INSERT INTO moderation_violations (nickname, room_id, content, violation_type, action, warning_count, created_at) VALUES (?,?,?,?,?,?,?)",
            (nickname, room_id, content, matched_category, "warning", new_count, now),
        )
        db.commit()
        return True, "warning", new_count, matched_category


# ── WebSocket 핸들러 ──────────────────────────────────────────────

@socketio.on("connect")
def handle_connect():
    emit("connected", {"message": "연결됨"})


@socketio.on("chat_message")
def handle_chat(data):
    room_id = data.get("room_id")
    nickname = data.get("nickname", "")
    content = data.get("content", "").strip()
    side = data.get("side", "red")
    role = data.get("role", "debater")

    if not room_id or not nickname or not content:
        return
    if side not in ("red", "blue"):
        side = "red"
    if role not in ("debater", "observer"):
        role = "debater"

    # 권한 확인
    user_role = get_user_role(room_id, nickname)
    if role == "debater":
        if user_role != f"debater_{side}":
            return
    else:
        if user_role != "observer":
            return

    db = get_db()
    room_row = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not room_row or room_row["status"] != "open":
        return

    # 비매너 검사
    blocked, action, warn_count, vtype = check_moderation(db, room_id, nickname, content, role)
    if blocked:
        if action == "suspended":
            # 정지 브로드캐스트
            socketio.emit("moderation_suspended", {
                "room_id": room_id,
                "nickname": nickname,
                "role": role,
            })
            # 본인에게만 상세
            emit("moderation_blocked", {
                "reason": "suspended",
                "message": "계정이 정지되었습니다. 관리자에게 문의하세요.",
            })
        else:
            # 경고 — 토론자는 모두에게, 관중은 본인에게만
            if role == "debater":
                socketio.emit("moderation_warning", {
                    "room_id": room_id,
                    "nickname": nickname,
                    "warning_count": warn_count,
                    "role": role,
                })
            emit("moderation_blocked", {
                "reason": "warning",
                "warning_count": warn_count,
                "message": "메시지를 전송할 수 없습니다. (경고 %d/3)" % warn_count,
            })
        return

    # 클라이언트 1차 차단된 메시지는 DB에 저장하지 않음
    if data.get("moderation_pre_blocked"):
        return

    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO messages (room_id, nickname, content, side, role, created_at) VALUES (?,?,?,?,?,?)",
        (room_id, nickname, content, side, role, now),
    )
    db.commit()

    msg = {
        "room_id": room_id,
        "nickname": nickname,
        "content": content,
        "side": side,
        "role": role,
        "created_at": now,
    }
    socketio.emit("new_message", msg)


@socketio.on("get_messages")
def handle_get_messages(data):
    room_id = data.get("room_id")
    if not room_id:
        return
    db = get_db()
    msgs = db.execute(
        "SELECT * FROM messages WHERE room_id=? ORDER BY created_at ASC", (room_id,)
    ).fetchall()
    emit("history", [dict(m) for m in msgs])


# ==================== 실행 ====================

def _init_background_tasks():
    """백그라운드 태스크 초기화 (서버 시작 시 한 번만 호출)."""
    if not hasattr(app, '_background_tasks_started'):
        app._background_tasks_started = True
        socketio.start_background_task(_deadline_scheduler)

# Gunicorn 또는 다른 WSGI 서버가 로드할 때
# /healthz는 제외 — 헬스체크가 SocketIO 초기화를 유발하면 Render가 재시작함
@app.before_request
def _ensure_background_tasks():
    if request.path != "/healthz":
        _init_background_tasks()

if __name__ == "__main__":
    # 직접 실행 시 (개발용)
    _init_background_tasks()
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    socketio.run(app, host="0.0.0.0", port=port, debug=debug, allow_unsafe_werkzeug=True)
