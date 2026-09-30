"""소규모 클래스/모임 신청 관리 프로그램

참가자: 모임 목록 보기, 신청, 취소
운영자: 비밀번호 확인 후 모임 등록/수정/삭제, 신청 현황 확인

데이터는 같은 폴더의 data.db (SQLite) 파일에 저장됩니다.
"""
import base64
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from functools import wraps

from flask import (Flask, g, render_template, request, redirect, url_for, flash, session, send_from_directory)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.txt")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}


def save_meetup_image(file_storage):
    """업로드된 이미지 파일을 uploads/에 저장하고 상대 경로를 반환."""
    if not file_storage or not file_storage.filename:
        return ""
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_IMAGE_EXT:
        flash("이미지 파일만 업로드할 수 있습니다. (png, jpg, gif, webp)")
        return ""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, filename))
    return f"/uploads/{filename}"


def save_dataurl_image(dataurl):
    """DataURL 문자열(data:image/png;base64,...)을 받아 uploads/에 저장하고 상대 경로를 반환."""
    if not dataurl or "," not in dataurl:
        return ""
    header, b64data = dataurl.split(",", 1)
    # 확장자 추출: data:image/png;base64 → png
    mime = header.split(":")[1].split(";")[0] if ":" in header else ""
    ext_map = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
    ext = ext_map.get(mime, "")
    if not ext:
        return ""
    try:
        raw = base64.b64decode(b64data)
    except Exception:
        return ""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(UPLOAD_DIR, filename), "wb") as f:
        f.write(raw)
    return f"/uploads/{filename}"


def parse_photos(raw):
    """meetups.photos 컬럼(JSON 문자열)을 리스트로 변환."""
    try:
        val = json.loads(raw) if raw else []
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def process_photo_upload(request, existing_photos, max_count=5):
    """다중 사진 업로드 처리.

    - 기존 사진 목록에서 삭제 요청(remove_photo_N)이 있으면 제거
    - 새 파일(photos[])을 max_count까지 업로드
    - 첫 번째 사진을 image_url(썸네일)로 설정
    반환: (photos_list, image_url)
    """
    photos = list(existing_photos)

    # 삭제 처리
    for i in range(max_count):
        key = f"remove_photo_{i}"
        if request.form.get(key):
            idx = int(request.form[key])
            if 0 <= idx < len(photos):
                old_path = photos[idx]
                if old_path and old_path.startswith("/uploads/"):
                    fp = os.path.join(BASE_DIR, old_path.lstrip("/"))
                    if os.path.exists(fp):
                        try:
                            os.remove(fp)
                        except OSError:
                            pass
                del photos[idx]

    # 새 파일 업로드 (빈 파일 제외, 최대 개수 초과분은 조용히 무시)
    files = request.files.getlist("photos[]")
    for f in files:
        if not f or not f.filename:
            continue
        if len(photos) >= max_count:
            break
        path = save_meetup_image(f)
        if path:
            photos.append(path)

    # DataURL 기반 업로드 (브라우저 JS에서 FileReader로 변환한 이미지)
    dataurls = request.form.getlist("photo_data[]")
    for du in dataurls:
        if not du:
            continue
        if len(photos) >= max_count:
            break
        path = save_dataurl_image(du)
        if path:
            photos.append(path)

    image_url = photos[0] if photos else ""
    return photos, image_url

# 운영자 비밀번호: config.txt 에 "비밀번호" 한 줄로 적어두면 그 값을 씁니다.
# 파일이 없으면 초기 비밀번호 1234 를 씁니다.
DEFAULT_ADMIN_PASSWORD = "1234"


def get_admin_password():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            value = f.read().strip()
            if value:
                return value
    return DEFAULT_ADMIN_PASSWORD


app = Flask(__name__)
app.secret_key = "meetup-secret-key-change-me"


# ---------------------------------------------------------------------------
# 데이터베이스 연결
# ---------------------------------------------------------------------------
def get_db():
    """이번 요청 동안 사용할 데이터베이스 연결을 반환합니다."""
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    """처음 실행할 때 테이블을 만듭니다. 이미 있으면 그대로 둡니다."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS meetups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            start_at TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM' 형식
            place TEXT NOT NULL,
            capacity INTEGER NOT NULL,       -- 정원
            host_nickname TEXT NOT NULL,     -- 운영자 닉네임
            fee INTEGER NOT NULL DEFAULT 0,  -- 참가비 (원)
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meetup_id INTEGER NOT NULL,
            user_id INTEGER,                 -- 가입 회원이면 연결 (비회원 신청은 NULL)
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            created_at TEXT NOT NULL,
            cancelled_at TEXT,               -- 운영자가 삭제한 시각 (NULL = 활성)
            cancel_reason TEXT,              -- 삭제 사유
            FOREIGN KEY (meetup_id) REFERENCES meetups(id),
            UNIQUE (meetup_id, phone)        -- 같은 모임에 같은 전화번호 중복 불가
        )
        """
    )
    # 회원 계정 (일반 회원 / 주최자 / 어드민)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            nickname TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'general',   -- general / host / admin
            pw_hash TEXT,                           -- 비밀번호 해시 (NULL = 미설정)
            created_at TEXT NOT NULL
        )
        """
    )
    # 게시판
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            author_name TEXT NOT NULL,
            author_id INTEGER,                      -- 작성자 user id (비회원은 NULL)
            created_at TEXT NOT NULL
        )
        """
    )
    # 기존 데이터베이스에 컬럼이 없으면 추가 (마이그레이션)
    cols = {r[1] for r in cur.execute("PRAGMA table_info(applications)").fetchall()}
    if "cancelled_at" not in cols:
        cur.execute("ALTER TABLE applications ADD COLUMN cancelled_at TEXT")
    if "cancel_reason" not in cols:
        cur.execute("ALTER TABLE applications ADD COLUMN cancel_reason TEXT")
    post_cols = {r[1] for r in cur.execute("PRAGMA table_info(posts)").fetchall()}
    if "views" not in post_cols:
        cur.execute("ALTER TABLE posts ADD COLUMN views INTEGER DEFAULT 0")
    meetup_cols = {r[1] for r in cur.execute("PRAGMA table_info(meetups)").fetchall()}
    if "views" not in meetup_cols:
        cur.execute("ALTER TABLE meetups ADD COLUMN views INTEGER DEFAULT 0")
    if "image_url" not in meetup_cols:
        cur.execute("ALTER TABLE meetups ADD COLUMN image_url TEXT DEFAULT ''")
    if "photos" not in meetup_cols:
        cur.execute("ALTER TABLE meetups ADD COLUMN photos TEXT DEFAULT '[]'")
    if "user_id" not in cols:
        cur.execute("ALTER TABLE applications ADD COLUMN user_id INTEGER")
    # users 테이블 마이그레이션
    ucols = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "role" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'general'")
    if "pw_hash" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN pw_hash TEXT")
    if "username" not in ucols:
        cur.execute("ALTER TABLE users ADD COLUMN username TEXT")
        # 기존 회원에게 username 자동 부여 (닉네임 또는 id 기반)
        rows = cur.execute("SELECT id, nickname FROM users WHERE username IS NULL").fetchall()
        for r in rows:
            uid, nick = r[0], r[1]
            uname = (nick or f"user{uid}").strip()[:20]
            uname = re.sub(r'[^a-zA-Z0-9_]', '', uname) or f"user{uid}"
            cur.execute("UPDATE users SET username = ? WHERE id = ?", (uname, uid))
        # 유니크 인덱스 생성 (중복 방지)
        try:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        except Exception:
            pass
    # phone UNIQUE 제약 제거 (아이디 기반 로그인 전환 후 전화번호는 더 이상 식별자 아님)
    # SQLite는 테이블 정의의 UNIQUE를 인덱스 드롭으로 제거할 수 없으므로 테이블 재구성
    # (users_new가 이미 존재하면 이전 실행에서 재구성이 완료된 것이므로 건너뜀)
    existing_tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "users_new" not in existing_tables:
        try:
            cur.execute("""
                CREATE TABLE users_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT DEFAULT '',
                    nickname TEXT NOT NULL,
                    username TEXT,
                    role TEXT NOT NULL DEFAULT 'general',
                    pw_hash TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                INSERT INTO users_new (id, name, phone, nickname, username, role, pw_hash, created_at)
                SELECT id, name, phone, nickname, username, role, pw_hash, created_at FROM users
            """)
            cur.execute("DROP TABLE users")
            cur.execute("ALTER TABLE users_new RENAME TO users")
            try:
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
            except Exception:
                pass
        except Exception:
            pass
    # meetups 테이블 마이그레이션
    mcols = {r[1] for r in cur.execute("PRAGMA table_info(meetups)").fetchall()}
    if "host_user_id" not in mcols:
        cur.execute("ALTER TABLE meetups ADD COLUMN host_user_id INTEGER")
    if "created_at" not in mcols:
        cur.execute("ALTER TABLE meetups ADD COLUMN created_at TEXT")
    # 까페 장소 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cafes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            district TEXT NOT NULL,
            road_address TEXT,
            phone TEXT,
            x_coord REAL,
            y_coord REAL
        )
        """
    )
    # 주최자 신청 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS host_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
            applied_at TEXT NOT NULL,
            decided_at TEXT,
            activity_area TEXT DEFAULT '',
            hosting_experience TEXT DEFAULT '',
            activity_plan TEXT DEFAULT ''
        )
        """
    )
    # 기존 DB에 새 컬럼 추가 (이미 존재하면 무시)
    for col in ["activity_area", "hosting_experience", "activity_plan"]:
        try:
            cur.execute(f"ALTER TABLE host_applications ADD COLUMN {col} TEXT DEFAULT ''")
        except Exception:
            pass
    # 활성 어드민 세션 플래그 (id=1 존재 = 어드민 로그인 중)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS active_admins (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            login_at TEXT NOT NULL
        )
        """
    )
    # 결제(코인 충전 신청) 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
            created_at TEXT NOT NULL,
            decided_at TEXT
        )
        """
    )
    # 코인 내역 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS coins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            change INTEGER NOT NULL,       -- +충전 / -차감
            balance INTEGER NOT NULL,      -- 사후 잔액
            reason TEXT NOT NULL,          -- charge / fee
            meetup_id INTEGER,             -- fee일 때 관련 모임
            created_at TEXT NOT NULL
        )
        """
    )
    # 모임 좋아요 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS meetup_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            meetup_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, meetup_id)
        )
        """
    )
    # 모집 종료 플래그
    try:
        cur.execute("ALTER TABLE meetups ADD COLUMN recruit_closed INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    conn.close()
    _load_cafes_if_empty()


def _extract_district(address):
    """주소에서 자치구(구/시/군)를 추출합니다. 예: '서울특별시 종로구 ...' -> '종로구'"""
    addr = (address or "").replace("서울특별시", "")
    m = re.search(r"[가-힣]+(?:구|시|군)", addr)
    return m.group(0) if m else ""


def _load_cafes_if_empty():
    """까페 테이블이 비어 있으면 같은 폴더의 CSV 파일을 읽어 넣습니다."""
    csv_path = os.path.join(BASE_DIR, "까페_영업중_서울.csv")
    if not os.path.exists(csv_path):
        return
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM cafes").fetchone()[0]
    if count > 0:
        conn.close()
        return
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            district = _extract_district(r.get("도로명주소", ""))
            x = r.get("좌표정보(X)", "").strip()
            y = r.get("좌표정보(Y)", "").strip()
            rows.append((
                r.get("사업장명", "").strip(),
                district,
                r.get("도로명주소", "").strip(),
                r.get("전화번호", "").strip(),
                float(x) if x else None,
                float(y) if y else None,
            ))
    conn.executemany(
        "INSERT INTO cafes (name, district, road_address, phone, x_coord, y_coord) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 유틸리티
# ---------------------------------------------------------------------------
def parse_datetime(value):
    """날짜/시간 문자열을 날짜 객체로 변환합니다.

    'YYYY-MM-DD HH:MM'(공백)과 'YYYY-MM-DDTHH:MM'(HTML datetime-local)
    두 형식 모두 받아들입니다.
    """
    value = value.strip().replace("T", " ")
    # 초가 붙어 있으면 ('HH:MM:SS') 잘라내어 분 단위만 남김
    if len(value) >= 8 and value[10] == ":":
        value = value[:16]
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


def format_fee(fee):
    """5000 -> '5,000원', 0 -> '무료'"""
    if not fee:
        return "무료"
    return f"{fee:,}원"


def clean_phone(raw):
    """전화번호를 정리하고 형식을 엄격히 검사합니다.

    '010-0000-0000' (앞 3자리 - 가운데 4자리 - 뒤 4자리) 형식만 허용합니다.
    하이픈을 제외한 모든 것이 숫자여야 하고, 숫자 총 11자리여야 유효합니다.
    유효하면 '-' 로 정리된 문자열을, 아니면 None 을 반환합니다.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    # 하이픈을 제외한 모든 것이 숫자여야 함
    digits_only = raw.replace("-", "")
    if not digits_only.isdigit():
        return None
    # 010-0000-0000 형식이므로 숫자는 정확히 11자리여야 함
    if len(digits_only) != 11:
        return None
    return f"{digits_only[:3]}-{digits_only[3:7]}-{digits_only[7:]}"


# 전화번호 입력창용 정규식 (브라우저 pattern 속성). 010-0000-0000 형식.
PHONE_PATTERN = r"01\d-?\d{4}-?\d{4}"

# 일반 이용자 세션 키
USER_SESSION_KEY = "user_id"


def current_user():
    """로그인한 일반 이용자를 반환 (없으면 None)."""
    uid = session.get(USER_SESSION_KEY)
    if not uid:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def current_user_id():
    """로그인한 회원의 id만 반환 (없으면 None). DB 조회 없이 session에서 바로."""
    return session.get(USER_SESSION_KEY)


# 템플릿에서 쓸 값(전화번호 pattern, 로그인한 회원)을 모든 화면에 노출
@app.context_processor
def inject_globals():
    return {
        "PHONE_PATTERN": PHONE_PATTERN,
        "current_user": current_user,
        "parse_photos": parse_photos,
    }


@app.template_filter("photos")
def photos_filter(raw):
    """Jinja2 필터: {{ meetup.photos|photos }}"""
    return parse_photos(raw)


def is_ended(meetup):
    """모임 시작 시각이 지났으면 True (종료된 모임)."""
    try:
        return parse_datetime(meetup["start_at"]) < datetime.now()
    except (ValueError, TypeError):
        return False


def count_applications(db, meetup_id):
    """운영자가 삭제하지 않은 활성 신청 수만 셉니다."""
    row = db.execute(
        "SELECT COUNT(*) AS n FROM applications WHERE meetup_id = ? AND cancelled_at IS NULL",
        (meetup_id,),
    ).fetchone()
    return row["n"]


# 운영자 선택할 수 있는 삭제 사유 목록
CANCEL_REASONS = [
    "불참 확인",
    "연락 두절",
    "부적절한 내용",
    "중복/오신청",
    "기타",
]


def login_required(view):
    """운영자 모드 화면만 보호하는 장식자(decorator).

    세션에 admin 이 없으면 비밀번호 입력 화면으로 보냅니다.
    """
    from functools import wraps as _wraps

    @_wraps(view)
    def wrapper(*args, **kwargs):
        from flask import session
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# 회원 (가입 / 로그인 / 로그아웃) — 역할: general / host
# ---------------------------------------------------------------------------
ROLE_LABELS = {"general": "일반 회원", "host": "주최자", "admin": "관리자"}


@app.route("/join", methods=["GET", "POST"])
def join():
    """회원 가입. 이름 + 닉네임 + 전화번호. 무조건 일반 회원으로 가입."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        nickname = request.form.get("nickname", "").strip()
        phone_raw = request.form.get("phone", "").strip()
        if not name or not nickname or not phone_raw:
            flash("모든 항목을 입력해 주세요.")
            return render_template("join.html")
        phone = clean_phone(phone_raw)
        if phone is None:
            flash("전화번호를 010-0000-0000 형식으로 입력해 주세요.")
            return render_template("join.html")
        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()
        if existing:
            flash("이미 가입된 전화번호입니다.")
            return render_template("join.html")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = db.execute(
            "INSERT INTO users (name, nickname, phone, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, nickname, phone, "general", now),
        )
        db.commit()
        session[USER_SESSION_KEY] = cur.lastrowid
        flash(f"환영합니다, {name}님! (일반 회원 가입 완료)")
        return redirect(url_for("index"))
    return render_template("join.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """회원 로그인 (전화번호)."""
    if request.method == "POST":
        phone_raw = request.form.get("phone", "").strip()
        if not phone_raw:
            flash("전화번호를 입력해 주세요.")
            return render_template("login.html")
        phone = clean_phone(phone_raw)
        if phone is None:
            flash("전화번호를 010-0000-0000 형식으로 입력해 주세요.")
            return render_template("login.html")
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        if row is None:
            flash("가입되지 않은 전화번호입니다.")
            return render_template("login.html")
        # 일반/주최자 로그인: 모든 어드민 세션 무효화
        db.execute("DELETE FROM active_admins")
        db.commit()
        session[USER_SESSION_KEY] = row["id"]
        flash(f"로그인했습니다, {row['name']}님! ({ROLE_LABELS.get(row['role'], '')})")
        # 역할별 리다이렉트
        if row["role"] == "host":
            return redirect(url_for("host_index"))
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    """로그아웃."""
    uid = session.get(USER_SESSION_KEY)
    if uid:
        db = get_db()
        row = db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
        if row and row["role"] == "admin":
            db.execute("DELETE FROM active_admins WHERE admin_id = ?", (uid,))
            db.commit()
    session.pop(USER_SESSION_KEY, None)
    flash("로그아웃했습니다.")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# 회원 페이지 (내 정보 / 내 신청 기록 / 탈퇴)
# ---------------------------------------------------------------------------
@app.route("/account")
def account():
    """회원 페이지. 내 정보 + 내 신청 기록 + 취소 사유 표시."""
    user = current_user()
    if user is None:
        flash("로그인이 필요합니다.")
        return redirect(url_for("login"))
    db = get_db()
    # 내 신청 기록 (활성 + 삭제된 것 모두, 삭제된 건 사유와 함께)
    apps = db.execute(
        """
        SELECT a.*, m.title AS meetup_title, m.start_at AS meetup_start, m.place
        FROM applications a
        JOIN meetups m ON m.id = a.meetup_id
        WHERE a.phone = ?
        ORDER BY a.created_at DESC
        """,
        (user["phone"],),
    ).fetchall()
    app_list = []
    for a in apps:
        item = dict(a)
        item["is_ended"] = is_ended({"start_at": a["meetup_start"]})
        item["is_cancelled"] = bool(a["cancelled_at"])
        app_list.append(item)
    # 주최자 신청 상태 조회 (가장 최근 건)
    host_app = db.execute(
        "SELECT * FROM host_applications WHERE user_id = ? ORDER BY applied_at DESC LIMIT 1",
        (user["id"],),
    ).fetchone()
    host_app_status = host_app["status"] if host_app else None
    # 코인 잔액 (최근 내역의 balance, 없으면 0)
    coin_row = db.execute(
        "SELECT balance FROM coins WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user["id"],),
    ).fetchone()
    coin_balance = coin_row["balance"] if coin_row else 0
    # 코인 내역 (최신 20건)
    coin_history = db.execute(
        """SELECT c.*, m.title AS meetup_title FROM coins c
           LEFT JOIN meetups m ON m.id = c.meetup_id
           WHERE c.user_id = ? ORDER BY c.id DESC LIMIT 20""",
        (user["id"],),
    ).fetchall()
    # 결제 대기 중인지 확인
    pending_payment = db.execute(
        "SELECT * FROM payments WHERE user_id = ? AND status = 'pending' LIMIT 1",
        (user["id"],),
    ).fetchone()
    return render_template("account.html", user=user, my_apps=app_list,
                           host_app_status=host_app_status,
                           coin_balance=coin_balance, coin_history=coin_history,
                           pending_payment=pending_payment)


@app.route("/account/pay", methods=["POST"])
def account_pay():
    """코인 충전 결제 신청."""
    user = current_user()
    if user is None:
        flash("로그인이 필요합니다.")
        return redirect(url_for("login"))
    amount = request.form.get("amount", "").strip()
    try:
        amount = int(amount)
    except (ValueError, TypeError):
        flash("금액을 숫자로 입력해 주세요.")
        return redirect(url_for("account"))
    if amount < 100 or amount > 1000000:
        flash("충전 금액은 100원 ~ 1,000,000원 사이여야 합니다.")
        return redirect(url_for("account"))
    db = get_db()
    existing = db.execute(
        "SELECT id FROM payments WHERE user_id = ? AND status = 'pending'",
        (user["id"],),
    ).fetchone()
    if existing:
        flash("이미 심사 중인 결제가 있습니다.")
        return redirect(url_for("account"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO payments (user_id, amount, status, created_at) VALUES (?, ?, 'pending', ?)",
        (user["id"], amount, now),
    )
    db.commit()
    flash(f"{amount:,}원 결제가 접수되었습니다. 관리자 승인 후 코인이 충전됩니다.")
    return redirect(url_for("account"))


@app.route("/account/apply-host", methods=["POST"])
def account_apply_host():
    """주최자 되기 신청."""
    user = current_user()
    if user is None:
        flash("로그인이 필요합니다.")
        return redirect(url_for("login"))
    if user["role"] != "general":
        flash("이미 주최자 권한이 있습니다.")
        return redirect(url_for("account"))
    db = get_db()
    existing = db.execute(
        "SELECT id FROM host_applications WHERE user_id = ? AND status = 'pending'",
        (user["id"],),
    ).fetchone()
    if existing:
        flash("이미 심사 중인 신청이 있습니다.")
        return redirect(url_for("account"))
    # 3개 필드 필수 확인
    activity_area = request.form.get("activity_area", "").strip()
    hosting_experience = request.form.get("hosting_experience", "").strip()
    activity_plan = request.form.get("activity_plan", "").strip()
    if not activity_area or not hosting_experience or not activity_plan:
        flash("주 활동 분야, 주최 경력, 활동 계획을 모두 입력해 주세요.")
        return redirect(url_for("account"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        """INSERT INTO host_applications (user_id, status, applied_at, activity_area, hosting_experience, activity_plan)
           VALUES (?, 'pending', ?, ?, ?, ?)""",
        (user["id"], now, activity_area, hosting_experience, activity_plan),
    )
    db.commit()
    flash("주최자 신청이 접수되었습니다. 관리자 승인 후 모임 생성 권한이 부여됩니다.")
    return redirect(url_for("account"))


@app.route("/account/delete", methods=["POST"])
def account_delete():
    """회원 탈퇴. 내 계정과 연결된 신청 기록도 함께 삭제."""
    user = current_user()
    if user is None:
        return redirect(url_for("login"))
    db = get_db()
    if user["role"] == "admin":
        db.execute("DELETE FROM active_admins WHERE admin_id = ?", (user["id"],))
    db.execute("DELETE FROM applications WHERE user_id = ?", (user["id"],))
    db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    db.commit()
    session.pop(USER_SESSION_KEY, None)
    flash("탈퇴가 완료되었습니다.")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# 참가자 화면
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """모임 목록. 정렬 필터: 최신순 / 조회수순 / 좋아요순."""
    db = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 정렬 파라미터
    sort = request.args.get("sort", "newest")  # newest / views / likes
    if sort == "views":
        order_clause = "m.views DESC"
    elif sort == "likes":
        order_clause = "(SELECT COUNT(*) FROM meetup_likes ml WHERE ml.meetup_id = m.id) DESC"
    else:
        order_clause = "m.created_at DESC"

    rows = db.execute(
        f"""
        SELECT m.*,
               (SELECT COUNT(*) FROM applications a
                WHERE a.meetup_id = m.id AND a.cancelled_at IS NULL) AS applied,
               (SELECT COUNT(*) FROM meetup_likes ml
                WHERE ml.meetup_id = m.id) AS like_count
        FROM meetups m
        ORDER BY {order_clause}
        """
    ).fetchall()

    upcoming, ended = [], []
    for r in rows:
        item = dict(r)
        item["is_ended"] = r["start_at"] < now
        item["recruit_closed"] = bool(r["recruit_closed"])
        item["applied"] = r["applied"]
        item["full"] = r["applied"] >= r["capacity"]
        item["like_count"] = r["like_count"]
        (ended if item["is_ended"] else upcoming).append(item)

    # 현재 유저의 좋아요 상태
    user = current_user()
    liked_ids = set()
    if user:
        liked_rows = db.execute(
            "SELECT meetup_id FROM meetup_likes WHERE user_id = ?", (user["id"],)
        ).fetchall()
        liked_ids = {r["meetup_id"] for r in liked_rows}

    return render_template(
        "index.html", upcoming=upcoming, ended=ended, user=user,
        sort=sort, liked_ids=liked_ids,
    )


@app.route("/meetup/<int:meetup_id>/like", methods=["POST"])
def toggle_like(meetup_id):
    """좋아요 토글 (로그인 필수). JSON 반환."""
    from flask import jsonify
    user = current_user()
    if not user:
        return jsonify({"ok": False, "msg": "로그인이 필요합니다."}), 401
    db = get_db()
    existing = db.execute(
        "SELECT id FROM meetup_likes WHERE user_id=? AND meetup_id=?",
        (user["id"], meetup_id),
    ).fetchone()
    if existing:
        db.execute("DELETE FROM meetup_likes WHERE id=?", (existing["id"],))
        liked = False
    else:
        db.execute(
            "INSERT INTO meetup_likes (user_id, meetup_id, created_at) VALUES (?, ?, ?)",
            (user["id"], meetup_id, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        liked = True
    db.commit()
    count = db.execute(
        "SELECT COUNT(*) AS c FROM meetup_likes WHERE meetup_id=?", (meetup_id,)
    ).fetchone()["c"]
    return jsonify({"ok": True, "liked": liked, "count": count})


@app.route("/api/cafes")
def api_cafes():
    """가게명·주소·지역 중 어떤 단어가든 포함하면 매칭되는 까페를 반환합니다."""
    from flask import jsonify
    db = get_db()
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    rows = db.execute(
        """SELECT name, road_address, phone FROM cafes
           WHERE name LIKE ? OR road_address LIKE ? OR district LIKE ?
           ORDER BY name LIMIT 30""",
        (like, like, like),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/meetup/<int:meetup_id>")
def detail(meetup_id):
    """모임 상세 + 신청 폼 + 내 신청 여부 표시."""
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if meetup is None:
        flash("존재하지 않는 모임입니다.")
        return redirect(url_for("index"))
    # 조회수 증가
    db.execute("UPDATE meetups SET views = COALESCE(views, 0) + 1 WHERE id = ?", (meetup_id,))
    db.commit()
    meetup = dict(meetup)
    meetup["views"] = (meetup["views"] or 0) + 1

    # 활성(운영자에게 삭제되지 않은) 신청만 공개
    apps = db.execute(
        "SELECT * FROM applications WHERE meetup_id = ? AND cancelled_at IS NULL "
        "ORDER BY created_at", (meetup_id,)
    ).fetchall()
    applied = len(apps)
    # 방문자의 전화번호: 로그인 회원이면 session에서, 아니면 URL 파라미터에서
    user = current_user()
    if user is not None:
        my_phone = user["phone"]
    else:
        my_phone = clean_phone(request.args.get("phone", "")) or ""
    already_applied = False
    my_cancelled = None
    my_app_row = None
    if my_phone:
        my_app = db.execute(
            "SELECT * FROM applications WHERE meetup_id = ? AND phone = ?",
            (meetup_id, my_phone),
        ).fetchone()
        if my_app:
            if my_app["cancelled_at"]:
                my_cancelled = dict(my_app)   # 운영자에 의해 삭제됨 → 사유 표시
            else:
                already_applied = True
                my_app_row = dict(my_app)     # 로그인 회원용: 내 신청 정보
    return render_template(
        "detail.html",
        meetup=dict(meetup),
        apps=[dict(a) for a in apps],
        applied=applied,
        full=applied >= meetup["capacity"],
        ended=is_ended(meetup),
        recruit_closed=bool(meetup["recruit_closed"]),
        my_phone=my_phone,
        already_applied=already_applied,
        my_cancelled=my_cancelled,
        my_app=my_app_row,
    )


@app.route("/meetup/<int:meetup_id>/apply", methods=["POST"])
def apply(meetup_id):
    """신청 처리. 정원·중복·종료 규칙을 모두 여기서 검사합니다."""
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if meetup is None:
        flash("존재하지 않는 모임입니다.")
        return redirect(url_for("index"))

    # 로그인한 회원만 신청할 수 있습니다
    user = current_user()
    if user is None:
        flash("신청하려면 먼저 로그인해 주세요.")
        return redirect(url_for("login"))
    name = user["name"]
    phone = user["phone"]

    if is_ended(meetup):
        flash("이미 종료된 모임이라 신청할 수 없습니다.")
        return redirect(url_for("detail", meetup_id=meetup_id))

    if meetup["recruit_closed"]:
        flash("모집이 종료되어 신청할 수 없습니다.")
        return redirect(url_for("detail", meetup_id=meetup_id))

    applied = count_applications(db, meetup_id)
    if applied >= meetup["capacity"]:
        flash("정원이 가득 차서 신청할 수 없습니다.")
        return redirect(url_for("detail", meetup_id=meetup_id))

    existing = db.execute(
        "SELECT 1 FROM applications WHERE meetup_id = ? AND phone = ?",
        (meetup_id, phone),
    ).fetchone()
    if existing:
        flash("이 전화번호는 이미 이 모임에 신청되어 있습니다.")
        return redirect(url_for("detail", meetup_id=meetup_id, phone=phone))

    # 활동비(코인) 확인 및 차감
    fee = meetup["fee"] or 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    if fee > 0:
        bal_row = db.execute(
            "SELECT balance FROM coins WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
        balance = bal_row["balance"] if bal_row else 0
        if balance < fee:
            flash(f"활동비({fee:,}코인)가 부족합니다. 현재 잔액: {balance:,}코인. 마이페이지에서 코인을 충전해 주세요.")
            return redirect(url_for("detail", meetup_id=meetup_id))
        new_balance = balance - fee
        db.execute(
            "INSERT INTO coins (user_id, change, balance, reason, meetup_id, created_at) VALUES (?, ?, ?, 'fee', ?, ?)",
            (user["id"], -fee, new_balance, meetup_id, now_str),
        )

    db.execute(
        """INSERT INTO applications (meetup_id, user_id, name, phone, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (meetup_id, user["id"], name, phone, now_str),
    )
    db.commit()
    if fee > 0:
        flash(f"'{meetup['title']}'에 신청되었습니다. 활동비 {fee:,}코인이 차감되었습니다.")
    else:
        flash(f"'{meetup['title']}'에 신청되었습니다. 감사합니다!")
    return redirect(url_for("detail", meetup_id=meetup_id, phone=phone))


@app.route("/meetup/<int:meetup_id>/cancel", methods=["GET", "POST"])
def cancel(meetup_id):
    """취소 전용 페이지(GET)와 실제 취소 처리(POST).

    GET : 전화번호를 받아 해당 신청 내용을 보여 주는 취소 확인 화면
    POST: [정말 취소하기] 클릭 시 실제 삭제 (화면에서는 confirm 팝업으로 이중 확인)
    """
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if meetup is None:
        flash("존재하지 않는 모임입니다.")
        return redirect(url_for("index"))

    phone_raw = (request.form.get("phone") or request.args.get("phone") or "").strip()
    phone = clean_phone(phone_raw) if phone_raw else None

    # GET : 취소 확인 화면 표시
    if request.method == "GET":
        app_row = None
        if phone:
            app_row = db.execute(
                "SELECT * FROM applications WHERE meetup_id = ? AND phone = ?",
                (meetup_id, phone),
            ).fetchone()
        return render_template(
            "cancel.html",
            meetup=dict(meetup),
            phone=phone_raw,
            app=dict(app_row) if app_row else None,
            ended=is_ended(meetup),
        )

    # POST : 실제 취소
    if not phone:
        flash("올바른 형식으로 입력해주세요 (예: 010-0000-0000).")
        return redirect(url_for("cancel", meetup_id=meetup_id, phone=phone_raw))

    if is_ended(meetup):
        flash("이미 종료된 모임이라 취소할 수 없습니다.")
        return redirect(url_for("detail", meetup_id=meetup_id, phone=phone))

    # 삭제 전 신청 정보 조회 (환불용)
    app_row = db.execute(
        "SELECT * FROM applications WHERE meetup_id = ? AND phone = ?", (meetup_id, phone)
    ).fetchone()

    cur = db.execute(
        "DELETE FROM applications WHERE meetup_id = ? AND phone = ?", (meetup_id, phone)
    )
    if cur.rowcount == 0:
        db.commit()
        flash("해당 전화번호의 신청 기록을 찾을 수 없습니다.")
        return redirect(url_for("detail", meetup_id=meetup_id, phone=phone))

    # 활동비 환불
    fee = meetup["fee"] or 0
    refunded = False
    if fee > 0 and app_row:
        user_id = app_row["user_id"]
        bal_row = db.execute(
            "SELECT balance FROM coins WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        balance = bal_row["balance"] if bal_row else 0
        new_balance = balance + fee
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT INTO coins (user_id, change, balance, reason, meetup_id, created_at) VALUES (?, ?, ?, 'refund', ?, ?)",
            (user_id, fee, new_balance, meetup_id, now_str),
        )
        refunded = True

    db.commit()
    if refunded:
        flash(f"신청이 취소되었습니다. 활동비 {fee:,}코인이 환불되었습니다.")
    else:
        flash("신청이 취소되었습니다.")
    return redirect(url_for("detail", meetup_id=meetup_id, phone=phone))


# ---------------------------------------------------------------------------
# 주최자 화면 (/host/*) — 나만의 모임 생성·관리
# ---------------------------------------------------------------------------
def host_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        uid = current_user_id()
        if not uid:
            return redirect(url_for("login"))
        db = get_db()
        row = db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
        if not row or row["role"] != "host":
            flash("주최자만 접근할 수 있습니다.")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/host")
@host_required
def host_index():
    """주최자의 내 모임 목록."""
    uid = current_user_id()
    db = get_db()
    meetups = db.execute(
        """SELECT m.*,
                  (SELECT COUNT(*) FROM applications a
                   WHERE a.meetup_id = m.id AND a.cancelled_at IS NULL) AS applied
           FROM meetups m WHERE m.host_user_id = ? ORDER BY m.start_at DESC""",
        (uid,),
    ).fetchall()
    for i, item in enumerate(meetups):
        d = dict(item)
        d["is_ended"] = is_ended(d)
        d["recruit_closed"] = bool(d.get("recruit_closed"))
        d["full"] = d["applied"] >= d["capacity"]
        meetups[i] = d
    return render_template("host_index.html", meetups=meetups)


@app.route("/host/meetup/new", methods=["GET", "POST"])
@host_required
def host_new_meetup():
    """주최자가 새 모임을 등록."""
    uid = current_user_id()
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        start_raw = request.form.get("start_at", "").strip()
        place = request.form.get("place", "").strip()
        capacity_raw = request.form.get("capacity", "").strip()
        fee_raw = request.form.get("fee", "0").strip()
        description = request.form.get("description", "").strip()
        photos, image_url = process_photo_upload(request, [])
        try:
            start_dt = datetime.strptime(start_raw, "%Y-%m-%dT%H:%M")
            capacity = int(capacity_raw)
            fee = int(fee_raw) if fee_raw else 0
        except ValueError:
            flash("날짜/시간, 정원, 참가비를 올바르게 입력해 주세요.")
            return render_template("host_form.html", meetup=None, user=user)
        if not title or not place or capacity < 1:
            flash("제목, 장소, 정원을 올바르게 입력해 주세요.")
            return render_template("host_form.html", meetup=None, user=user)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = db.execute(
            """INSERT INTO meetups (title, start_at, place, capacity, fee,
               host_nickname, description, image_url, photos, created_at, host_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, start_dt.strftime("%Y-%m-%d %H:%M:%S"), place, capacity, fee,
             user["nickname"], description, image_url, json.dumps(photos), now, uid),
        )
        db.commit()
        flash("모임이 등록되었습니다.")
        return redirect(url_for("host_index"))
    return render_template("host_form.html", meetup=None, user=user)


@app.route("/host/meetup/<int:meetup_id>/edit", methods=["GET", "POST"])
@host_required
def host_edit_meetup(meetup_id):
    """주최자가 자신의 모임 수정."""
    uid = current_user_id()
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if not meetup or meetup["host_user_id"] != uid:
        flash("해당 모임을 찾을 수 없습니다.")
        return redirect(url_for("host_index"))
    user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        start_raw = request.form.get("start_at", "").strip()
        place = request.form.get("place", "").strip()
        capacity_raw = request.form.get("capacity", "").strip()
        fee_raw = request.form.get("fee", "0").strip()
        description = request.form.get("description", "").strip()
        existing_photos = parse_photos(meetup["photos"])
        photos, image_url = process_photo_upload(request, existing_photos)
        try:
            start_dt = datetime.strptime(start_raw, "%Y-%m-%dT%H:%M")
            capacity = int(capacity_raw)
            fee = int(fee_raw) if fee_raw else 0
        except ValueError:
            flash("날짜/시간, 정원, 참가비를 올바르게 입력해 주세요.")
            return render_template("host_form.html", meetup=dict(meetup), user=user)
        if not title or not place or capacity < 1:
            flash("제목, 장소, 정원을 올바르게 입력해 주세요.")
            return render_template("host_form.html", meetup=dict(meetup), user=user)
        db.execute(
            """UPDATE meetups SET title=?, start_at=?, place=?, capacity=?, fee=?,
               description=?, image_url=?, photos=? WHERE id=?""",
            (title, start_dt.strftime("%Y-%m-%d %H:%M:%S"), place, capacity, fee,
             description, image_url, json.dumps(photos), meetup_id),
        )
        db.commit()
        flash("모임이 수정되었습니다.")
        return redirect(url_for("host_index"))
    # 신청자 목록 (회원관리용) — 삭제되지 않은 신청만
    apps = db.execute(
        """SELECT a.id AS app_id, u.name, u.nickname, u.phone, a.created_at
           FROM applications a JOIN users u ON a.user_id = u.id
           WHERE a.meetup_id = ? AND a.cancelled_at IS NULL
           ORDER BY a.created_at""",
        (meetup_id,),
    ).fetchall()
    # 삭제된 신청자 목록 (복구 가능)
    removed_apps = db.execute(
        """SELECT a.id AS app_id, u.name, u.nickname, u.phone,
                  a.cancel_reason, a.cancelled_at
           FROM applications a JOIN users u ON a.user_id = u.id
           WHERE a.meetup_id = ? AND a.cancelled_at IS NOT NULL
           ORDER BY a.cancelled_at DESC""",
        (meetup_id,),
    ).fetchall()
    return render_template(
        "host_form.html", meetup=dict(meetup), user=user,
        apps=apps, removed_apps=removed_apps,
    )


@app.route("/host/meetup/<int:meetup_id>/delete", methods=["POST"])
@host_required
def host_delete_meetup(meetup_id):
    """주최자가 자신의 모임 삭제."""
    uid = current_user_id()
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if not meetup or meetup["host_user_id"] != uid:
        flash("해당 모임을 찾을 수 없습니다.")
        return redirect(url_for("host_index"))
    db.execute("DELETE FROM applications WHERE meetup_id = ?", (meetup_id,))
    db.execute("DELETE FROM meetups WHERE id = ?", (meetup_id,))
    db.commit()
    flash("모임이 삭제되었습니다.")
    return redirect(url_for("host_index"))


@app.route("/host/meetup/<int:meetup_id>/toggle-recruit", methods=["POST"])
@host_required
def host_toggle_recruit(meetup_id):
    """모집 종료/재개 토글."""
    uid = current_user_id()
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if not meetup or meetup["host_user_id"] != uid:
        flash("해당 모임을 찾을 수 없습니다.")
        return redirect(url_for("host_index"))
    new_val = 0 if meetup["recruit_closed"] else 1
    db.execute("UPDATE meetups SET recruit_closed = ? WHERE id = ?", (new_val, meetup_id))
    db.commit()
    flash("모집을 재개했습니다." if new_val == 0 else "모집을 종료했습니다.")
    return redirect(request.referrer or url_for("host_edit_meetup", meetup_id=meetup_id))


@app.route("/host/meetup/<int:meetup_id>/applications")
@host_required
def host_applications(meetup_id):
    """주최자가 자신이 만든 모임의 신청 현황 확인."""
    uid = current_user_id()
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if not meetup or meetup["host_user_id"] != uid:
        flash("해당 모임을 찾을 수 없습니다.")
        return redirect(url_for("host_index"))
    apps = db.execute(
        "SELECT * FROM applications WHERE meetup_id = ? AND cancelled_at IS NULL ORDER BY created_at",
        (meetup_id,),
    ).fetchall()
    return render_template("host_applications.html", meetup=dict(meetup), apps=apps)


@app.route("/host/meetup/<int:meetup_id>/application/<int:application_id>/delete", methods=["POST"])
@host_required
def host_delete_app(meetup_id, application_id):
    """주최자가 자신이 만든 모임의 신청자 삭제."""
    uid = current_user_id()
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if not meetup or meetup["host_user_id"] != uid:
        flash("해당 모임을 찾을 수 없습니다.")
        return redirect(url_for("host_index"))
    # 삭제 사유 (드롭다운 + 상세 서술)
    reason = request.form.get("cancel_reason", "").strip()
    detail = request.form.get("cancel_detail", "").strip()
    full_reason = reason if not detail else f"{reason} — {detail}"
    db.execute(
        "UPDATE applications SET cancelled_at = ?, cancel_reason = ? WHERE id = ? AND meetup_id = ?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), full_reason or "주최자 삭제", application_id, meetup_id),
    )
    db.commit()
    flash("신청자가 삭제되었습니다.")
    return redirect(url_for("host_edit_meetup", meetup_id=meetup_id))


@app.route("/host/meetup/<int:meetup_id>/application/<int:application_id>/restore", methods=["POST"])
@host_required
def host_restore_app(meetup_id, application_id):
    """주최자가 삭제된 신청자 복구."""
    uid = current_user_id()
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if not meetup or meetup["host_user_id"] != uid:
        flash("해당 모임을 찾을 수 없습니다.")
        return redirect(url_for("host_index"))
    db.execute(
        "UPDATE applications SET cancelled_at = NULL, cancel_reason = NULL WHERE id = ? AND meetup_id = ?",
        (application_id, meetup_id),
    )
    db.commit()
    flash("신청자가 복구되었습니다.")
    return redirect(url_for("host_edit_meetup", meetup_id=meetup_id))


# ---------------------------------------------------------------------------
# 어드민 화면 (/admin/*) — 모든 모임·회원·게시판 관리
# ---------------------------------------------------------------------------
ADMIN_PW = os.environ.get("ADMIN_PASSWORD", "1234")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """어드민 전용 로그인 (비밀번호 직접 입력)."""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hashlib.sha256(pw.encode()).hexdigest() != hashlib.sha256(get_admin_password().encode()).hexdigest():
            flash("관리자 비밀번호가 올바르지 않습니다.")
            return render_template("admin_login.html")
        session.pop(USER_SESSION_KEY, None)
        session["admin_auth"] = True
        # active_admins 플래그 설정 → 일반 회원 로그인 시 삭제되어 자동 로그아웃
        db = get_db()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT OR REPLACE INTO active_admins (id, login_at) VALUES (1, ?)",
            (now,),
        )
        db.commit()
        flash("관리자로 로그인했습니다.")
        return redirect(url_for("admin_index"))
    # 이미 어드민 세션이 있으면 대시보드로
    if session.get("admin_auth"):
        return redirect(url_for("admin_index"))
    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["GET", "POST"])
def admin_logout():
    """어드민 로그아웃."""
    session.pop("admin_auth", None)
    db = get_db()
    db.execute("DELETE FROM active_admins WHERE id = 1")
    db.commit()
    flash("관리자에서 로그아웃했습니다.")
    return redirect(url_for("admin_login"))


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_auth"):
            return redirect(url_for("admin_login"))
        # active_admins 플래그 확인 — 일반 회원 로그인 시 삭제되어 자동 로그아웃
        db = get_db()
        flag = db.execute("SELECT 1 FROM active_admins WHERE id = 1").fetchone()
        if not flag:
            session.pop("admin_auth", None)
            flash("관리자 세션이 만료되었습니다. 다시 로그인해 주세요.")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/admin/change-password", methods=["GET", "POST"])
@admin_required
def admin_change_password():
    """관리자 비밀번호 변경 — 기존 비밀번호 확인 필요."""
    if request.method == "POST":
        current_pw = request.form.get("current_password", "").strip()
        new_pw = request.form.get("new_password", "").strip()
        confirm_pw = request.form.get("confirm_password", "").strip()

        # 기존 비밀번호 확인
        stored_pw = get_admin_password()
        if hashlib.sha256(current_pw.encode()).hexdigest() != hashlib.sha256(stored_pw.encode()).hexdigest():
            flash("기존 비밀번호가 일치하지 않습니다.")
            return render_template("admin_change_password.html")

        # 새 비밀번호 검증
        if len(new_pw) < 4:
            flash("새 비밀번호는 4자 이상이어야 합니다.")
            return render_template("admin_change_password.html")
        if new_pw != confirm_pw:
            flash("새 비밀번호와 확인이 일치하지 않습니다.")
            return render_template("admin_change_password.html")

        # config.txt에 저장
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(new_pw)
        flash("비밀번호가 변경되었습니다.")
        return redirect(url_for("admin_index"))

    return render_template("admin_change_password.html")


@app.route("/admin")
@admin_required
def admin_index():
    """어드민 대시보드 — 전체 모임 + 회원 요약."""
    db = get_db()
    meetups = db.execute(
        """SELECT m.*, u.nickname AS host_name,
                  (SELECT COUNT(*) FROM applications a
                   WHERE a.meetup_id = m.id AND a.cancelled_at IS NULL) AS applied
           FROM meetups m LEFT JOIN users u ON m.host_user_id = u.id
           ORDER BY m.start_at DESC"""
    ).fetchall()
    for i, item in enumerate(meetups):
        d = dict(item)
        d["is_ended"] = is_ended(d)
        d["full"] = d["applied"] >= d["capacity"]
        meetups[i] = d
    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    general_count = db.execute("SELECT COUNT(*) FROM users WHERE role='general'").fetchone()[0]
    host_count = db.execute("SELECT COUNT(*) FROM users WHERE role='host'").fetchone()[0]
    post_count = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    pending_payments = db.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
    return render_template(
        "admin_index.html", meetups=meetups,
        total_users=total_users, general_count=general_count,
        host_count=host_count, post_count=post_count,
        pending_payments=pending_payments,
    )


@app.route("/admin/payments")
@admin_required
def admin_payments():
    """결제 내역 목록."""
    db = get_db()
    rows = db.execute(
        """SELECT p.*, u.name, u.nickname, u.phone
           FROM payments p JOIN users u ON p.user_id = u.id
           ORDER BY p.created_at DESC"""
    ).fetchall()
    payment_list = [dict(r) for r in rows]
    return render_template("admin_payments.html", payments=payment_list)


@app.route("/admin/payments/<int:pay_id>/approve", methods=["POST"])
@app.route("/admin/payments/<int:pay_id>/reject", methods=["POST"])
@admin_required
def admin_decide_payment(pay_id):
    """결제 승인(코인 충전)/거절."""
    db = get_db()
    # form의 _action 필드로 분기 (승인/거절 버튼이 같은 URL을 사용)
    action = request.form.get("_action", "")
    is_approve = action == "approve"
    new_status = "approved" if is_approve else "rejected"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pay = db.execute(
        """SELECT p.*, u.name, u.nickname FROM payments p
           JOIN users u ON p.user_id = u.id WHERE p.id = ?""",
        (pay_id,),
    ).fetchone()
    if not pay or pay["status"] != "pending":
        flash("이미 처리된 결제입니다.")
        return redirect(url_for("admin_payments"))
    db.execute(
        "UPDATE payments SET status = ?, decided_at = ? WHERE id = ?",
        (new_status, now, pay_id),
    )
    if new_status == "approved":
        # 현재 잔액 조회 후 코인 충전
        bal_row = db.execute(
            "SELECT balance FROM coins WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (pay["user_id"],),
        ).fetchone()
        current_balance = bal_row["balance"] if bal_row else 0
        new_balance = current_balance + pay["amount"]
        db.execute(
            "INSERT INTO coins (user_id, change, balance, reason, created_at) VALUES (?, ?, ?, 'charge', ?)",
            (pay["user_id"], pay["amount"], new_balance, now),
        )
        flash(f"{pay['nickname']}님에게 {pay['amount']:,}코인이 충전되었습니다.")
    else:
        flash(f"{pay['nickname']}님의 결제가 거절되었습니다.")
    db.commit()
    return redirect(url_for("admin_payments"))


@app.route("/admin/users")
@admin_required
def admin_users():
    """전체 회원 목록 (역할별)."""
    db = get_db()
    role_filter = request.args.get("role", "")
    if role_filter in ("general", "host"):
        rows = db.execute("SELECT * FROM users WHERE role = ? ORDER BY created_at DESC", (role_filter,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    user_list = []
    for u in rows:
        d = dict(u)
        d["meetup_count"] = db.execute(
            "SELECT COUNT(*) FROM meetups WHERE host_user_id = ?", (d["id"],)
        ).fetchone()[0]
        user_list.append(d)
    general_count = db.execute("SELECT COUNT(*) FROM users WHERE role = 'general'").fetchone()[0]
    host_count = db.execute("SELECT COUNT(*) FROM users WHERE role = 'host'").fetchone()[0]
    pending_host_apps = db.execute(
        "SELECT ha.*, u.name, u.nickname, u.phone "
        "FROM host_applications ha JOIN users u ON ha.user_id = u.id "
        "WHERE ha.status = 'pending' ORDER BY ha.applied_at DESC"
    ).fetchall()
    return render_template("admin_users.html", users=user_list, active_role=role_filter,
                           general_count=general_count, host_count=host_count,
                           pending_host_apps=pending_host_apps)


@app.route("/admin/host-apps/<int:app_id>/approve", methods=["POST"])
@app.route("/admin/host-apps/<int:app_id>/reject", methods=["POST"])
@admin_required
def admin_decide_host_app(app_id):
    """주최자 신청 승인/거절."""
    db = get_db()
    # form의 _action 필드로 분기 (승인/거절 버튼이 같은 URL을 사용)
    action = request.form.get("_action", "")
    is_approve = action == "approve"
    new_status = "approved" if is_approve else "rejected"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    app_row = db.execute("SELECT * FROM host_applications WHERE id = ?", (app_id,)).fetchone()
    if not app_row or app_row["status"] != "pending":
        flash("이미 처리된 신청입니다.")
        return redirect(url_for("admin_users"))
    db.execute(
        "UPDATE host_applications SET status = ?, decided_at = ? WHERE id = ?",
        (new_status, now, app_id),
    )
    if new_status == "approved":
        db.execute("UPDATE users SET role = 'host' WHERE id = ?", (app_row["user_id"],))
        flash("주최자 신청이 승인되었습니다. 해당 회원은 이제 모임을 만들 수 있습니다.")
    else:
        flash("주최자 신청이 거절되었습니다.")
    db.commit()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    """회원 삭제 (본인 어드민 계정은 삭제 불가)."""
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        flash("회원을 찾을 수 없습니다.")
        return redirect(url_for("admin_users"))
    if target["role"] == "admin":
        flash("관리자 계정은 삭제할 수 없습니다.")
        return redirect(url_for("admin_users"))
    # 주최자라면 그 모임을 먼저 정리 (신청 → 모임 순서)
    if target["role"] == "host":
        host_meetup_ids = [r[0] for r in db.execute(
            "SELECT id FROM meetups WHERE host_user_id = ?", (user_id,)
        ).fetchall()]
        for mid in host_meetup_ids:
            db.execute("DELETE FROM applications WHERE meetup_id = ?", (mid,))
        if host_meetup_ids:
            placeholders = ",".join("?" * len(host_meetup_ids))
            db.execute(f"DELETE FROM meetups WHERE id IN ({placeholders})", host_meetup_ids)
    db.execute("DELETE FROM applications WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    extra = f" (모임 {len(host_meetup_ids)}개 포함)" if target["role"] == "host" and host_meetup_ids else ""
    flash(f"{target['nickname']}님을 삭제했습니다{extra}.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_change_role(user_id):
    """회원 역할 변경."""
    new_role = request.form.get("role", "")
    if new_role not in ("general", "host"):
        flash("올바른 역할이 아닙니다.")
        return redirect(url_for("admin_users"))
    db = get_db()
    db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    db.commit()
    flash("역할을 변경했습니다.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>")
@admin_required
def admin_user_detail(user_id):
    """회원 상세정보: 가입 정보, 가입된 모임, 주최한 모임, 결제 정보."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("해당 회원을 찾을 수 없습니다.")
        return redirect(url_for("admin_users"))
    # 가입된 모임 (신청자로서)
    joined_meetups = db.execute(
        """SELECT m.id, m.title, m.start_at, m.place, a.cancelled_at, a.cancel_reason
           FROM applications a JOIN meetups m ON a.meetup_id = m.id
           WHERE a.user_id = ? ORDER BY m.start_at DESC""",
        (user_id,),
    ).fetchall()
    # 주최한 모임
    hosted_meetups = db.execute(
        """SELECT m.id, m.title, m.start_at, m.place, m.capacity,
                  (SELECT COUNT(*) FROM applications WHERE meetup_id = m.id AND cancelled_at IS NULL) AS applied
           FROM meetups m WHERE m.host_user_id = ? ORDER BY m.start_at DESC""",
        (user_id,),
    ).fetchall()
    # 결제 정보
    payments = db.execute(
        "SELECT * FROM payments WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    # 코인 잔액
    coin_row = db.execute(
        "SELECT COALESCE(SUM(change), 0) AS balance FROM coins WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return render_template(
        "admin_user_detail.html",
        user=dict(user),
        joined_meetups=[dict(r) for r in joined_meetups],
        hosted_meetups=[dict(r) for r in hosted_meetups],
        payments=[dict(p) for p in payments],
        coin_balance=coin_row["balance"] if coin_row else 0,
    )


@app.route("/admin/meetup/new", methods=["GET", "POST"])
@admin_required
def admin_new():
    """어드민이 새 모임을 등록."""
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        start_raw = request.form.get("start_at", "").strip()
        place = request.form.get("place", "").strip()
        capacity_raw = request.form.get("capacity", "").strip()
        fee_raw = request.form.get("fee", "0").strip()
        description = request.form.get("description", "").strip()
        host_nickname = request.form.get("host_nickname", "").strip() or "관리자"
        photos, image_url = process_photo_upload(request, [])
        try:
            start_dt = datetime.strptime(start_raw, "%Y-%m-%dT%H:%M")
            capacity = int(capacity_raw)
            fee = int(fee_raw) if fee_raw else 0
        except ValueError:
            flash("날짜/시간, 정원, 참가비를 올바르게 입력해 주세요.")
            return render_template("admin_form.html", meetup=None)
        if not title or not place or capacity < 1:
            flash("제목, 장소, 정원을 올바르게 입력해 주세요.")
            return render_template("admin_form.html", meetup=None)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            """INSERT INTO meetups (title, start_at, place, capacity, fee,
               host_nickname, description, image_url, photos, created_at, host_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (title, start_dt.strftime("%Y-%m-%d %H:%M:%S"), place, capacity, fee,
             host_nickname, description, image_url, json.dumps(photos), now),
        )
        db.commit()
        flash("모임이 등록되었습니다.")
        return redirect(url_for("admin_index"))
    return render_template("admin_form.html", meetup=None)


@app.route("/admin/meetup/<int:meetup_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_meetup(meetup_id):
    """어드민이 기존 모임 수정."""
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if not meetup:
        flash("해당 모임을 찾을 수 없습니다.")
        return redirect(url_for("admin_index"))
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        start_raw = request.form.get("start_at", "").strip()
        place = request.form.get("place", "").strip()
        capacity_raw = request.form.get("capacity", "").strip()
        fee_raw = request.form.get("fee", "0").strip()
        description = request.form.get("description", "").strip()
        host_nickname = request.form.get("host_nickname", "").strip() or "관리자"
        existing_photos = parse_photos(meetup["photos"])
        photos, image_url = process_photo_upload(request, existing_photos)
        try:
            start_dt = datetime.strptime(start_raw, "%Y-%m-%dT%H:%M")
            capacity = int(capacity_raw)
            fee = int(fee_raw) if fee_raw else 0
        except ValueError:
            flash("날짜/시간, 정원, 참가비를 올바르게 입력해 주세요.")
            return render_template("admin_form.html", meetup=dict(meetup))
        if not title or not place or capacity < 1:
            flash("제목, 장소, 정원을 올바르게 입력해 주세요.")
            return render_template("admin_form.html", meetup=dict(meetup))
        db.execute(
            """UPDATE meetups SET title=?, start_at=?, place=?, capacity=?, fee=?,
               host_nickname=?, description=?, image_url=?, photos=? WHERE id=?""",
            (title, start_dt.strftime("%Y-%m-%d %H:%M:%S"), place, capacity, fee,
             host_nickname, description, image_url, json.dumps(photos), meetup_id),
        )
        db.commit()
        flash("모임이 수정되었습니다.")
        return redirect(url_for("admin_index"))
    # 신청자 목록 조회 (수정 화면에서 확인/삭제 가능)
    apps = db.execute(
        "SELECT * FROM applications WHERE meetup_id = ? AND cancelled_at IS NULL ORDER BY created_at",
        (meetup_id,),
    ).fetchall()
    return render_template(
        "admin_form.html",
        meetup=dict(meetup),
        apps=[dict(a) for a in apps],
        applied=len(apps),
        recruit_closed=bool(meetup["recruit_closed"]),
    )


@app.route("/admin/meetup/<int:meetup_id>/toggle-recruit", methods=["POST"])
@admin_required
def admin_toggle_recruit(meetup_id):
    """어드민이 모집 중단/재개 토글."""
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if not meetup:
        flash("해당 모임을 찾을 수 없습니다.")
        return redirect(url_for("admin_index"))
    new_val = 0 if meetup["recruit_closed"] else 1
    db.execute("UPDATE meetups SET recruit_closed = ? WHERE id = ?", (new_val, meetup_id))
    db.commit()
    flash("모집을 재개했습니다." if new_val == 0 else "모집을 중단했습니다.")
    return redirect(request.referrer or url_for("admin_edit_meetup", meetup_id=meetup_id))


@app.route("/admin/meetup/<int:meetup_id>/delete", methods=["POST"])
@admin_required
def admin_delete_meetup(meetup_id):
    """어드민이任意 모임 삭제."""
    db = get_db()
    db.execute("DELETE FROM applications WHERE meetup_id = ?", (meetup_id,))
    db.execute("DELETE FROM meetups WHERE id = ?", (meetup_id,))
    db.commit()
    flash("모임이 삭제되었습니다.")
    return redirect(url_for("admin_index"))


@app.route("/admin/board")
@admin_required
def admin_board():
    """게시판 글 관리."""
    db = get_db()
    posts = db.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()
    return render_template("admin_board.html", posts=posts)


@app.route("/admin/board/<int:post_id>/delete", methods=["POST"])
@admin_required
def admin_delete_post(post_id):
    """게시글 삭제."""
    db = get_db()
    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    db.commit()
    flash("게시글이 삭제되었습니다.")
    return redirect(url_for("admin_board"))


@app.route("/admin/board/write", methods=["GET", "POST"])
@admin_required
def admin_write_post():
    """어드민 게시글 작성 (별도 화면)."""
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        if not title or not content:
            flash("제목과 내용을 모두 입력해 주세요.")
            return render_template("admin_board_write.html")
        db = get_db()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO posts (title, content, author_name, author_id, created_at) VALUES (?, ?, '관리자', NULL, ?)",
            (title, content, now),
        )
        db.commit()
        flash("공지가 등록되었습니다.")
        return redirect(url_for("admin_board"))
    return render_template("admin_board_write.html")


@app.route("/admin/board/<int:post_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_post(post_id):
    """관리자 공지 수정."""
    db = get_db()
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        flash("공지를 찾을 수 없습니다.")
        return redirect(url_for("admin_board"))
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        if not title or not content:
            flash("제목과 내용을 모두 입력해 주세요.")
            return render_template("admin_board_write.html", post=dict(post))
        db.execute(
            "UPDATE posts SET title = ?, content = ? WHERE id = ?",
            (title, content, post_id),
        )
        db.commit()
        flash("공지가 수정되었습니다.")
        return redirect(url_for("admin_board"))
    return render_template("admin_board_write.html", post=dict(post))


# ---------------------------------------------------------------------------
# 업로드된 이미지 서빙
# ---------------------------------------------------------------------------
@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# 게시판 (일반 이용자용 /board)
# ---------------------------------------------------------------------------
@app.route("/board")
def board_index():
    """게시판 글 목록."""
    db = get_db()
    posts = db.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()
    return render_template("board_index.html", posts=posts)


@app.route("/board/<int:post_id>")
def board_detail(post_id):
    """게시글 상세."""
    db = get_db()
    row = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not row:
        flash("게시글을 찾을 수 없습니다.")
        return redirect(url_for("board_index"))
    # 조회수 증가 (SQL에서 +1 처리 완료)
    db.execute("UPDATE posts SET views = COALESCE(views, 0) + 1 WHERE id = ?", (post_id,))
    db.commit()
    post = dict(row)
    post["views"] = (post["views"] or 0) + 1
    return render_template("board_detail.html", post=post)


@app.route("/board/write", methods=["GET", "POST"])
@admin_required
def board_write():
    """게시글 작성 (어드민만)."""
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        if not title or not content:
            flash("제목과 내용을 모두 입력해 주세요.")
            return render_template("board_write.html")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO posts (title, content, author_name, author_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (title, content, "관리자", None, now),
        )
        db.commit()
        flash("게시글이 등록되었습니다.")
        return redirect(url_for("board_index"))
    return render_template("board_write.html")


@app.route("/admin/meetup/<int:meetup_id>/applications")
@admin_required
def admin_applications(meetup_id):
    """어드민이 특정 모임의 신청자 명단 확인."""
    db = get_db()
    meetup = db.execute("SELECT * FROM meetups WHERE id = ?", (meetup_id,)).fetchone()
    if meetup is None:
        flash("존재하지 않는 모임입니다.")
        return redirect(url_for("admin_index"))
    apps = db.execute(
        "SELECT * FROM applications WHERE meetup_id = ? AND cancelled_at IS NULL "
        "ORDER BY created_at", (meetup_id,)
    ).fetchall()
    return render_template(
        "admin_applications.html",
        meetup=dict(meetup),
        apps=[dict(a) for a in apps],
        applied=len(apps),
        ended=is_ended(meetup),
    )


@app.route("/admin/application/<int:application_id>/delete", methods=["POST"])
@admin_required
def admin_delete_application(application_id):
    """어드민이 특정 신청자를 삭제(사유 선택)."""
    db = get_db()
    app_row = db.execute(
        "SELECT * FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    if app_row is None or app_row["cancelled_at"]:
        flash("해당 신청을 찾을 수 없습니다(이미 삭제되었을 수 있음).")
        return redirect(url_for("admin_index"))

    if request.method == "POST":
        reason = request.form.get("reason", "").strip()
        if reason not in CANCEL_REASONS:
            flash("삭제 사유를 선택해 주세요.")
            return redirect(url_for("admin_delete_application", application_id=application_id))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "UPDATE applications SET cancelled_at = ?, cancel_reason = ? WHERE id = ?",
            (now, reason, application_id),
        )
        db.commit()
        flash(f"신청자를 삭제했습니다. (사유: {reason})")
        return redirect(url_for("admin_applications", meetup_id=app_row["meetup_id"]))

    return render_template(
        "admin_delete_application.html",
        app=dict(app_row),
        reasons=CANCEL_REASONS,
    )


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
