#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
جامبو آزمون (Jumbo Exam) - آزمون‌ساز حرفه‌ای تک‌فایل Flask
ویژگی‌ها:
- ثبت‌نام و ورود طراحان
- آپلود فایل .q و ساخت آزمون
- لینک اختصاصی برای دانش‌آموزان
- ورود اطلاعات دانش‌آموز + تایمر
- نمره‌دهی تستی دقیق + تشابه متنی (۷۲٪ به بالا)
- داشبورد کامل با نمودار و آمار
"""

import os
import re
import json
import uuid
import sqlite3
import hashlib
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from functools import wraps
from io import BytesIO

from flask import (
    Flask, request, render_template_string, redirect, url_for,
    session, g, jsonify, send_file, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ============================================================
# تنظیمات
# ============================================================
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-exam-maker-2024-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exam_maker.db")
SIMILARITY_THRESHOLD = 0.72  # ۷۲٪

# ============================================================
# پایگاه داده
# ============================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS exams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        unique_code TEXT UNIQUE NOT NULL,
        duration_minutes INTEGER DEFAULT 30,
        questions_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_active INTEGER DEFAULT 1,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_id INTEGER NOT NULL,
        student_name TEXT NOT NULL,
        student_family TEXT NOT NULL,
        student_phone TEXT,
        student_national_id TEXT,
        answers_json TEXT,
        score REAL DEFAULT 0,
        max_score REAL DEFAULT 0,
        percentage REAL DEFAULT 0,
        started_at TIMESTAMP,
        finished_at TIMESTAMP,
        time_spent_seconds INTEGER,
        details_json TEXT,
        FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_exams_code ON exams(unique_code);
    CREATE INDEX IF NOT EXISTS idx_attempts_exam ON attempts(exam_id);
    """)
    db.commit()

# ============================================================
# پارس فایل .q
# ============================================================
def parse_q_file(content: str):
    """
    فرمت پشتیبانی‌شده:
    &: سوال
    - گزینه عادی
    * گزینه صحیح
    #-
    \"\"\"
    پاسخ تشریحی
    \"\"\"
    {
    img:"https://...";
    }
    """
    questions = []
    # جدا کردن بلوک‌های سوال
    blocks = re.split(r'(?=^&:)', content, flags=re.MULTILINE)
    for block in blocks:
        block = block.strip()
        if not block.startswith("&:"):
            continue

        q = {
            "text": "",
            "options": [],
            "correct_index": None,
            "explanation": "",
            "image": None,
            "type": "mcq"  # یا descriptive
        }

        # سوال
        m = re.match(r'^&:\s*(.+?)(?=\n-|\#-|\n\*|\Z)', block, re.DOTALL)
        if m:
            q["text"] = m.group(1).strip()

        # گزینه‌ها
        option_lines = re.findall(r'^([*-])\s*(.+)$', block, re.MULTILINE)
        correct_idx = None
        for i, (marker, text) in enumerate(option_lines):
            q["options"].append(text.strip())
            if marker == "*":
                correct_idx = i
        q["correct_index"] = correct_idx

        # پایان گزینه‌ها
        if "#-" in block or "-#" in block:
            pass

        # پاسخ تشریحی
        exp_m = re.search(r'"""(.*?)"""', block, re.DOTALL)
        if exp_m:
            q["explanation"] = exp_m.group(1).strip()

        # تصویر
        img_m = re.search(r'\{\s*img\s*:\s*["\']([^"\']+)["\']\s*;?\s*\}', block, re.IGNORECASE)
        if img_m:
            q["image"] = img_m.group(1).strip()

        # تشخیص نوع
        if not q["options"]:
            q["type"] = "descriptive"
        elif q["correct_index"] is None and q["explanation"]:
            # اگر گزینه هست ولی * نداره، از توضیح سعی می‌کنیم حدس بزنیم (اختیاری)
            q["type"] = "mcq"
        else:
            q["type"] = "mcq"

        if q["text"]:
            questions.append(q)

    return questions

def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    a = re.sub(r'\s+', ' ', a.strip().lower())
    b = re.sub(r'\s+', ' ', b.strip().lower())
    return SequenceMatcher(None, a, b).ratio()

# ============================================================
# احراز هویت
# ============================================================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:

            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

# ============================================================
# قالب‌های HTML (RTL + Bootstrap 5 + Chart.js)
# ============================================================
BASE_STYLE = """
<style>
  :root {
    --primary: #4f46e5;
    --primary-dark: #4338ca;
    --success: #10b981;
    --danger: #ef4444;
    --warning: #f59e0b;
    --bg: #f8fafc;
    --card: #ffffff;
    --text: #1e293b;
    --muted: #64748b;
  }
  * { box-sizing: border-box; }
  body {
    font-family: 'Vazirmatn', 'Tahoma', sans-serif;
    background: linear-gradient(135deg, #e0e7ff 0%, #f0f9ff 50%, #fdf4ff 100%);
    min-height: 100vh;
    color: var(--text);
    direction: rtl;
  }
  .navbar {
    background: linear-gradient(90deg, var(--primary), #7c3aed) !important;
    box-shadow: 0 4px 20px rgba(79,70,229,0.3);
  }
  .card {
    border: none;
    border-radius: 1rem;
    box-shadow: 0 10px 40px rgba(0,0,0,0.08);
    transition: transform 0.2s, box-shadow 0.2s;
  }
  .card:hover { transform: translateY(-2px); box-shadow: 0 15px 50px rgba(0,0,0,0.12); }
  .btn-primary {
    background: linear-gradient(135deg, var(--primary), #7c3aed);
    border: none;
    border-radius: 0.75rem;
    padding: 0.6rem 1.5rem;
    font-weight: 600;
  }
  .btn-primary:hover { background: linear-gradient(135deg, var(--primary-dark), #6d28d9); }
  .btn-success { border-radius: 0.75rem; }
  .btn-outline-danger { border-radius: 0.75rem; }
  .form-control, .form-select {
    border-radius: 0.75rem;
    border: 2px solid #e2e8f0;
    padding: 0.7rem 1rem;
  }
  .form-control:focus { border-color: var(--primary); box-shadow: 0 0 0 0.2rem rgba(79,70,229,0.25); }
  .badge-score { font-size: 1.1rem; padding: 0.5rem 1rem; border-radius: 2rem; }
  .timer-box {
    background: linear-gradient(135deg, #fef3c7, #fde68a);
    border-radius: 1rem;
    padding: 1rem 1.5rem;
    font-size: 1.5rem;
    font-weight: 700;
    color: #92400e;
    text-align: center;
  }
  .question-card { border-right: 5px solid var(--primary); }
  .option-label {
    display: block;
    padding: 0.9rem 1.2rem;
    margin: 0.4rem 0;
    border: 2px solid #e2e8f0;
    border-radius: 0.75rem;
    cursor: pointer;
    transition: all 0.2s;
  }
  .option-label:hover { border-color: var(--primary); background: #eef2ff; }
  .option-label input:checked + span { font-weight: 700; color: var(--primary); }
  .stat-card {
    background: linear-gradient(135deg, #fff, #f8fafc);
    border-radius: 1rem;
    padding: 1.5rem;
    text-align: center;
  }
  .stat-value { font-size: 2rem; font-weight: 800; color: var(--primary); }
  .stat-label { color: var(--muted); font-size: 0.9rem; }
  footer { margin-top: 4rem; padding: 2rem; text-align: center; color: var(--muted); }
  .hero {
    background: linear-gradient(135deg, rgba(79,70,229,0.9), rgba(124,58,237,0.85));
    color: white;
    border-radius: 1.5rem;
    padding: 3rem 2rem;
    margin-bottom: 2rem;
  }
  .table th { background: #f1f5f9; }
  pre.q-format {
    background: #1e293b;
    color: #e2e8f0;
    padding: 1.5rem;
    border-radius: 1rem;
    font-size: 0.85rem;
    overflow-x: auto;
    direction: ltr;
    text-align: left;
  }
</style>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.rtl.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/Vazirmatn-font-face.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
"""

def render(page_title, content, user=None):
    nav = ""
    if user:
        nav = f"""
        <li class="nav-item"><a class="nav-link text-white" href="{url_for('dashboard')}">داشبورد</a></li>
        <li class="nav-item"><a class="nav-link text-white" href="{url_for('create_exam')}">ساخت آزمون</a></li>
        <li class="nav-item"><a class="nav-link text-white" href="{url_for('logout')}">خروج ({user['username']})</a></li>
        """
    else:
        nav = f"""
        <li class="nav-item"><a class="nav-link text-white" href="{url_for('login')}">ورود</a></li>
        <li class="nav-item"><a class="nav-link text-white" href="{url_for('register')}">ثبت‌نام</a></li>
        """
    return render_template_string(f"""
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{page_title} | جامبو آزمون</title>
  {BASE_STYLE}
</head>
<body>
  <nav class="navbar navbar-expand-lg navbar-dark mb-4">
    <div class="container">
      <a class="navbar-brand fw-bold" href="{url_for('index')}">📝 جامبو آزمون</a>
      <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nav">
        <span class="navbar-toggler-icon"></span>
      </button>
      <div class="collapse navbar-collapse" id="nav">
        <ul class="navbar-nav me-auto">{nav}</ul>
      </div>
    </div>
  </nav>
  <div class="container pb-5">
    {content}
  </div>
  <footer>
    <p>جامبو آزمون • ساخته شده با Flask • پشتیبانی از فایل .q</p>
  </footer>
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
""")

# ============================================================
# مسیرها
# ============================================================
@app.route("/")
def index():
    user = get_current_user()
    content = """
    <div class="hero text-center">
      <h1 class="display-4 fw-bold mb-3">جامبو آزمون</h1>
      <p class="lead mb-4">آزمون بساز، لینک بده، نتیجه را با نمودار و آمار کامل ببین</p>
      <div class="d-flex justify-content-center gap-3 flex-wrap">
        <a href="/register" class="btn btn-light btn-lg px-4">شروع رایگان</a>
        <a href="/login" class="btn btn-outline-light btn-lg px-4">ورود طراح</a>
      </div>
    </div>
    <div class="row g-4 mt-2">
      <div class="col-md-4">
        <div class="card h-100 p-4 text-center">
          <div style="font-size:3rem">📄</div>
          <h5 class="mt-3">آپلود فایل .q</h5>
          <p class="text-muted">سوالات تستی و تشریحی را با فرمت استاندارد آپلود کنید</p>
        </div>
      </div>
      <div class="col-md-4">
        <div class="card h-100 p-4 text-center">
          <div style="font-size:3rem">🔗</div>
          <h5 class="mt-3">لینک اختصاصی</h5>
          <p class="text-muted">برای هر آزمون لینک یکتا دریافت کنید و به دانش‌آموزان بدهید</p>
        </div>
      </div>
      <div class="col-md-4">
        <div class="card h-100 p-4 text-center">
          <div style="font-size:3rem">📊</div>
          <h5 class="mt-3">داشبورد پیشرفته</h5>
          <p class="text-muted">نمره، نمودار، میانگین، جزئیات کامل هر دانش‌آموز</p>
        </div>
      </div>
    </div>
    <div class="card mt-5 p-4">
      <h4 class="mb-3">فرمت فایل .q</h4>
      <pre class="q-format">
&: نماد شیمیایی طلا چیست؟
*Au
-Ag
-Gd
-Go
#-
\"\"\"
نماد Au از واژه لاتین Aurum گرفته شده است.
طلا فلزی واسطه با عدد اتمی ۷۹ است.
\"\"\"
{
img:"https://example.com/images/gold.png";
}

&: آب از چند عنصر تشکیل شده است؟
-1
*2
-3
-4
#-
\"\"\"
آب (H₂O) از دو عنصر هیدروژن و اکسیژن تشکیل شده است.
\"\"\"
{
img:"https://example.com/images/water.png";
}

&: توضیح دهید چرا طلا زرد است؟
#-
\"\"\"
به دلیل ساختار الکترونی خاص و جذب طول موج‌های خاص نور.
\"\"\"
      </pre>
      <p class="mt-3 text-muted small">
        • خط شروع سوال با <code>&:</code><br>
        • گزینه‌ها با <code>-</code> و گزینه صحیح با <code>*</code><br>
        • پایان گزینه‌ها با <code>#-</code><br>
        • پاسخ تشریحی داخل <code>\"\"\" ... \"\"\"</code><br>
        • تصویر اختیاری با <code>{img:"url";}</code><br>
        • سوالات بدون گزینه = تشریحی (نمره‌دهی بر اساس شباهت متنی ≥ ۷۲٪)
      </p>
    </div>
    """
    return render("خانه", content, user)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        if not username or not email or not password:
            return redirect(url_for("register"))
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, email, password_hash, full_name) VALUES (?,?,?,?)",
                (username, email, generate_password_hash(password), full_name)
            )
            db.commit()
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            return redirect(url_for("register"))

    content = """
    <div class="row justify-content-center">
      <div class="col-md-5">
        <div class="card p-4">
          <h3 class="text-center mb-4">ثبت‌نام طراح آزمون</h3>
          <form method="post">
            <div class="mb-3">
              <label class="form-label">نام کامل</label>
              <input type="text" name="full_name" class="form-control" placeholder="مثلاً علی رضایی">
            </div>
            <div class="mb-3">
              <label class="form-label">نام کاربری *</label>
              <input type="text" name="username" class="form-control" required>
            </div>
            <div class="mb-3">
              <label class="form-label">ایمیل *</label>
              <input type="email" name="email" class="form-control" required>
            </div>
            <div class="mb-3">
              <label class="form-label">رمز عبور *</label>
              <input type="password" name="password" class="form-control" required minlength="6">
            </div>
            <button type="submit" class="btn btn-primary w-100">ثبت‌نام</button>
          </form>
          <p class="text-center mt-3 mb-0">قبلاً ثبت‌نام کرده‌اید؟ <a href="/login">ورود</a></p>
        </div>
      </div>
    </div>
    """
    return render("ثبت‌نام", content)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]

            return redirect(url_for("dashboard"))

    content = """
    <div class="row justify-content-center">
      <div class="col-md-5">
        <div class="card p-4">
          <h3 class="text-center mb-4">ورود طراح</h3>
          <form method="post">
            <div class="mb-3">
              <label class="form-label">نام کاربری</label>
              <input type="text" name="username" class="form-control" required>
            </div>
            <div class="mb-3">
              <label class="form-label">رمز عبور</label>
              <input type="password" name="password" class="form-control" required>
            </div>
            <button type="submit" class="btn btn-primary w-100">ورود</button>
          </form>
          <p class="text-center mt-3 mb-0">حساب ندارید؟ <a href="/register">ثبت‌نام</a></p>
        </div>
      </div>
    </div>
    """
    return render("ورود", content)

@app.route("/logout")
def logout():
    session.clear()

    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    db = get_db()
    exams = db.execute(
        "SELECT e.*, (SELECT COUNT(*) FROM attempts a WHERE a.exam_id = e.id) as attempt_count "
        "FROM exams e WHERE e.user_id = ? ORDER BY e.created_at DESC",
        (user["id"],)
    ).fetchall()
    rows = ""
    for e in exams:
        status = '<span class="badge bg-success">فعال</span>' if e["is_active"] else '<span class="badge bg-secondary">غیرفعال</span>'
        link = url_for("take_exam", code=e["unique_code"], _external=True)
        rows += f"""
        <tr>
          <td><strong>{e['title']}</strong><br><small class="text-muted">{e['description'] or ''}</small></td>
          <td>{e['duration_minutes']} دقیقه</td>
          <td>{e['attempt_count']}</td>
          <td>{status}</td>
          <td>
            <a href="{url_for('exam_results', exam_id=e['id'])}" class="btn btn-sm btn-primary">نتایج</a>
            <button class="btn btn-sm btn-outline-secondary" onclick="navigator.clipboard.writeText('{link}');alert('لینک کپی شد!')">کپی لینک</button>
          </td>
        </tr>
        """
    content = f"""
    <div class="d-flex justify-content-between align-items-center mb-4">
      <h2>داشبورد من</h2>
      <a href="{url_for('create_exam')}" class="btn btn-primary">+ ساخت آزمون جدید</a>
    </div>
    <div class="card">
      <div class="card-body">
        <div class="table-responsive">
          <table class="table table-hover align-middle">
            <thead>
              <tr>
                <th>عنوان آزمون</th>
                <th>مدت</th>
                <th>تعداد شرکت‌کننده</th>
                <th>وضعیت</th>
                <th>عملیات</th>
              </tr>
            </thead>
            <tbody>
              {rows if rows else '<tr><td colspan="5" class="text-center text-muted py-4">هنوز آزمونی نساخته‌اید.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    """
    return render("داشبورد", content, user)

@app.route("/create", methods=["GET", "POST"])
@login_required
def create_exam():
    user = get_current_user()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        duration = int(request.form.get("duration", 30) or 30)
        file = request.files.get("qfile")
        if not title or not file:

            return redirect(url_for("create_exam"))
        content = file.read().decode("utf-8", errors="ignore")
        questions = parse_q_file(content)
        if not questions:

            return redirect(url_for("create_exam"))
        code = uuid.uuid4().hex[:10]
        db = get_db()
        db.execute(
            "INSERT INTO exams (user_id, title, description, unique_code, duration_minutes, questions_json) VALUES (?,?,?,?,?,?)",
            (user["id"], title, description, code, duration, json.dumps(questions, ensure_ascii=False))
        )
        db.commit()

        return redirect(url_for("dashboard"))
    content = f"""
    <div class="row justify-content-center">
      <div class="col-lg-7">
        <div class="card p-4">
          <h3 class="mb-4">ساخت آزمون جدید</h3>
          <form method="post" enctype="multipart/form-data">
            <div class="mb-3">
              <label class="form-label">عنوان آزمون *</label>
              <input type="text" name="title" class="form-control" required placeholder="مثلاً آزمون میان‌ترم شیمی">
            </div>
            <div class="mb-3">
              <label class="form-label">توضیحات</label>
              <textarea name="description" class="form-control" rows="2"></textarea>
            </div>
            <div class="mb-3">
              <label class="form-label">مدت زمان (دقیقه) *</label>
              <input type="number" name="duration" class="form-control" value="30" min="5" max="300" required>
            </div>
            <div class="mb-3">
              <label class="form-label">فایل سوالات (.q) *</label>
              <input type="file" name="qfile" class="form-control" accept=".q,.txt" required>
              <div class="form-text">فایل متنی با فرمت استاندارد .q</div>
            </div>
            <button type="submit" class="btn btn-primary w-100">ساخت آزمون و دریافت لینک</button>
          </form>
        </div>
      </div>
    </div>
    """
    return render("ساخت آزمون", content, user)

@app.route("/exam/<code>", methods=["GET", "POST"])
def take_exam(code):
    db = get_db()
    exam = db.execute("SELECT * FROM exams WHERE unique_code = ? AND is_active = 1", (code,)).fetchone()
    if not exam:
        abort(404)
    questions = json.loads(exam["questions_json"])

    if request.method == "POST":
        # شروع یا پایان
        action = request.form.get("action")
        if action == "start":
            session["exam_start"] = datetime.utcnow().isoformat()
            session["exam_code"] = code
            session["student"] = {
                "name": request.form.get("name", "").strip(),
                "family": request.form.get("family", "").strip(),
                "phone": request.form.get("phone", "").strip(),
                "national_id": request.form.get("national_id", "").strip(),
            }
            if not session["student"]["name"] or not session["student"]["family"]:

                return redirect(url_for("take_exam", code=code))
            return redirect(url_for("take_exam", code=code))

        if action == "submit":
            if session.get("exam_code") != code:

                return redirect(url_for("take_exam", code=code))
            student = session.get("student", {})
            start_str = session.get("exam_start")
            start_dt = datetime.fromisoformat(start_str) if start_str else datetime.utcnow()
            finished = datetime.utcnow()
            time_spent = int((finished - start_dt).total_seconds())

            answers = {}
            details = []
            score = 0.0
            max_score = float(len(questions))

            for i, q in enumerate(questions):
                key = f"q{i}"
                user_ans = request.form.get(key, "").strip()
                answers[key] = user_ans
                is_correct = False
                sim = 0.0
                if q["type"] == "mcq" and q["correct_index"] is not None:
                    try:
                        selected = int(user_ans)
                        is_correct = (selected == q["correct_index"])
                    except:
                        is_correct = False
                    if is_correct:
                        score += 1
                else:
                    # تشریحی یا بدون گزینه صحیح مشخص
                    correct_text = q.get("explanation", "")
                    sim = similarity(user_ans, correct_text)
                    is_correct = sim >= SIMILARITY_THRESHOLD
                    if is_correct:
                        score += 1
                details.append({
                    "q_index": i,
                    "user_answer": user_ans,
                    "is_correct": is_correct,
                    "similarity": round(sim * 100, 1) if q["type"] != "mcq" or q["correct_index"] is None else None,
                    "correct_index": q.get("correct_index"),
                    "explanation": q.get("explanation", "")
                })

            percentage = round((score / max_score) * 100, 1) if max_score else 0
            db.execute(
                """INSERT INTO attempts 
                   (exam_id, student_name, student_family, student_phone, student_national_id,
                    answers_json, score, max_score, percentage, started_at, finished_at, time_spent_seconds, details_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    exam["id"], student.get("name"), student.get("family"),
                    student.get("phone"), student.get("national_id"),
                    json.dumps(answers, ensure_ascii=False),
                    score, max_score, percentage,
                    start_dt.isoformat(), finished.isoformat(), time_spent,
                    json.dumps(details, ensure_ascii=False)
                )
            )
            db.commit()
            # پاک کردن session
            for k in ["exam_start", "exam_code", "student"]:
                session.pop(k, None)
            content = f"""
            <div class="card p-5 text-center">
              <div style="font-size:4rem">🎉</div>
              <h2 class="mt-3">آزمون با موفقیت ثبت شد</h2>
              <p class="lead">نمره شما: <span class="badge bg-primary badge-score">{score} از {max_score}</span></p>
              <p>درصد: <strong>{percentage}٪</strong></p>
              <p class="text-muted">نتایج کامل در اختیار طراح آزمون قرار گرفت.</p>
            </div>
            """
            return render("پایان آزمون", content)

    # صفحه ورود اطلاعات یا خود آزمون
    if session.get("exam_code") == code and session.get("exam_start"):
        # نمایش سوالات + تایمر
        start_dt = datetime.fromisoformat(session["exam_start"])
        end_dt = start_dt + timedelta(minutes=exam["duration_minutes"])
        remaining = max(0, int((end_dt - datetime.utcnow()).total_seconds()))
        q_html = ""
        for i, q in enumerate(questions):
            img = f'<img src="{q["image"]}" class="img-fluid rounded mb-3" style="max-height:200px" alt="تصویر سوال">' if q.get("image") else ""
            if q["type"] == "mcq" and q["options"]:
                opts = ""
                for j, opt in enumerate(q["options"]):
                    opts += f"""
                    <label class="option-label">
                      <input type="radio" name="q{i}" value="{j}" class="form-check-input me-2" required>
                      <span>{opt}</span>
                    </label>
                    """
                q_html += f"""
                <div class="card question-card p-4 mb-4">
                  <h5>سوال {i+1}</h5>
                  <p>{q['text']}</p>
                  {img}
                  {opts}
                </div>
                """
            else:
                q_html += f"""
                <div class="card question-card p-4 mb-4">
                  <h5>سوال {i+1} (تشریحی)</h5>
                  <p>{q['text']}</p>
                  {img}
                  <textarea name="q{i}" class="form-control" rows="4" placeholder="پاسخ خود را بنویسید..." required></textarea>
                </div>
                """
        content = f"""
        <div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
          <h3>{exam['title']}</h3>
          <div class="timer-box" id="timer">{remaining // 60:02d}:{remaining % 60:02d}</div>
        </div>
        <form method="post" id="examForm">
          <input type="hidden" name="action" value="submit">
          {q_html}
          <div class="text-center mt-4">
            <button type="submit" class="btn btn-success btn-lg px-5" onclick="return confirm('آیا از اتمام آزمون مطمئن هستید؟')">اتمام آزمون و ثبت پاسخ‌ها</button>
          </div>
        </form>
        <script>
          let remaining = {remaining};
          const timerEl = document.getElementById('timer');
          const form = document.getElementById('examForm');
          setInterval(() => {{
            remaining--;
            if (remaining <= 0) {{
              timerEl.textContent = '۰۰:۰۰';
              form.submit();
              return;
            }}
            const m = Math.floor(remaining / 60);
            const s = remaining % 60;
            timerEl.textContent = String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
            if (remaining < 60) timerEl.style.background = 'linear-gradient(135deg,#fecaca,#fca5a5)';
          }}, 1000);
        </script>
        """
        return render(exam["title"], content)
    else:
        # فرم ورود اطلاعات دانش‌آموز
        content = f"""
        <div class="row justify-content-center">
          <div class="col-md-6">
            <div class="card p-4">
              <h3 class="text-center mb-2">{exam['title']}</h3>
              <p class="text-center text-muted mb-4">{exam['description'] or ''}</p>
              <p class="text-center"><span class="badge bg-info">مدت زمان: {exam['duration_minutes']} دقیقه</span>
              <span class="badge bg-secondary">تعداد سوال: {len(questions)}</span></p>
              <form method="post">
                <input type="hidden" name="action" value="start">
                <div class="mb-3">
                  <label class="form-label">نام *</label>
                  <input type="text" name="name" class="form-control" required>
                </div>
                <div class="mb-3">
                  <label class="form-label">نام خانوادگی *</label>
                  <input type="text" name="family" class="form-control" required>
                </div>
                <div class="mb-3">
                  <label class="form-label">شماره تماس</label>
                  <input type="text" name="phone" class="form-control" placeholder="09xxxxxxxxx">
                </div>
                <div class="mb-3">
                  <label class="form-label">کد ملی</label>
                  <input type="text" name="national_id" class="form-control" maxlength="10">
                </div>
                <button type="submit" class="btn btn-primary w-100 btn-lg">شروع آزمون</button>
              </form>
            </div>
          </div>
        </div>
        """
        return render("ورود به آزمون", content)

@app.route("/results/<int:exam_id>")
@login_required
def exam_results(exam_id):
    user = get_current_user()
    db = get_db()
    exam = db.execute("SELECT * FROM exams WHERE id = ? AND user_id = ?", (exam_id, user["id"])).fetchone()
    if not exam:
        abort(404)
    attempts = db.execute(
        "SELECT * FROM attempts WHERE exam_id = ? ORDER BY finished_at DESC", (exam_id,)
    ).fetchall()
    questions = json.loads(exam["questions_json"])

    # آمار
    scores = [a["percentage"] for a in attempts]
    avg = round(sum(scores) / len(scores), 1) if scores else 0
    max_p = max(scores) if scores else 0
    min_p = min(scores) if scores else 0
    count = len(attempts)

    # جدول نتایج
    rows = ""
    for a in attempts:
        rows += f"""
        <tr>
          <td>{a['student_name']} {a['student_family']}</td>
          <td>{a['student_phone'] or '-'}</td>
          <td>{a['student_national_id'] or '-'}</td>
          <td><span class="badge bg-{'success' if a['percentage']>=70 else 'warning' if a['percentage']>=50 else 'danger'}">{a['score']}/{a['max_score']} ({a['percentage']}٪)</span></td>
          <td>{a['time_spent_seconds'] // 60}:{a['time_spent_seconds'] % 60:02d}</td>
          <td>{a['finished_at'][:19] if a['finished_at'] else '-'}</td>
          <td><a href="{url_for('attempt_detail', attempt_id=a['id'])}" class="btn btn-sm btn-outline-primary">جزئیات</a></td>
        </tr>
        """

    chart_data = json.dumps(scores)
    labels = json.dumps([f"{a['student_name']} {a['student_family'][0]}." for a in attempts])

    content = f"""
    <div class="d-flex justify-content-between align-items-center mb-4 flex-wrap gap-2">
      <div>
        <h2>{exam['title']}</h2>
        <p class="text-muted mb-0">نتایج و آمار کامل</p>
      </div>
      <a href="{url_for('dashboard')}" class="btn btn-outline-secondary">بازگشت</a>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-6 col-md-3">
        <div class="stat-card">
          <div class="stat-value">{count}</div>
          <div class="stat-label">تعداد شرکت‌کننده</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="stat-card">
          <div class="stat-value">{avg}٪</div>
          <div class="stat-label">میانگین نمرات</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="stat-card">
          <div class="stat-value">{max_p}٪</div>
          <div class="stat-label">بالاترین نمره</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="stat-card">
          <div class="stat-value">{min_p}٪</div>
          <div class="stat-label">پایین‌ترین نمره</div>
        </div>
      </div>
    </div>

    <div class="card p-4 mb-4">
      <h5 class="mb-3">نمودار نمرات</h5>
      <canvas id="scoreChart" height="100"></canvas>
    </div>

    <div class="card">
      <div class="card-body">
        <div class="table-responsive">
          <table class="table table-hover align-middle">
            <thead>
              <tr>
                <th>نام و نام خانوادگی</th>
                <th>تلفن</th>
                <th>کد ملی</th>
                <th>نمره</th>
                <th>زمان صرف‌شده</th>
                <th>زمان اتمام</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows if rows else '<tr><td colspan="7" class="text-center text-muted py-4">هنوز کسی در آزمون شرکت نکرده است.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <script>
      const ctx = document.getElementById('scoreChart');
      if (ctx) {{
        new Chart(ctx, {{
          type: 'bar',
          data: {{
            labels: {labels},
            datasets: [{{
              label: 'درصد نمره',
              data: {chart_data},
              backgroundColor: 'rgba(79, 70, 229, 0.7)',
              borderRadius: 8
            }}]
          }},
          options: {{
            responsive: true,
            plugins: {{ legend: {{ display: false }} }},
            scales: {{
              y: {{ beginAtZero: true, max: 100, ticks: {{ callback: v => v + '٪' }} }}
            }}
          }}
        }});
      }}
    </script>
    """
    return render("نتایج آزمون", content, user)

@app.route("/attempt/<int:attempt_id>")
@login_required
def attempt_detail(attempt_id):
    user = get_current_user()
    db = get_db()
    attempt = db.execute(
        """SELECT a.*, e.title, e.user_id, e.questions_json 
           FROM attempts a JOIN exams e ON a.exam_id = e.id 
           WHERE a.id = ?""", (attempt_id,)
    ).fetchone()
    if not attempt or attempt["user_id"] != user["id"]:
        abort(404)
    questions = json.loads(attempt["questions_json"])
    details = json.loads(attempt["details_json"] or "[]")
    answers = json.loads(attempt["answers_json"] or "{}")

    detail_html = ""
    for d in details:
        i = d["q_index"]
        q = questions[i]
        status = '<span class="badge bg-success">صحیح</span>' if d["is_correct"] else '<span class="badge bg-danger">غلط</span>'
        sim_info = f'<br><small>شباهت: {d["similarity"]}٪</small>' if d.get("similarity") is not None else ""
        user_ans_display = d["user_answer"]
        if q["type"] == "mcq" and q["options"] and d["user_answer"].isdigit():
            idx = int(d["user_answer"])
            user_ans_display = q["options"][idx] if 0 <= idx < len(q["options"]) else d["user_answer"]
        correct_display = ""
        if q["correct_index"] is not None and q["options"]:
            correct_display = f'<p class="mb-1"><strong>پاسخ صحیح:</strong> {q["options"][q["correct_index"]]}</p>'
        detail_html += f"""
        <div class="card p-3 mb-3 {'border-success' if d['is_correct'] else 'border-danger'}">
          <div class="d-flex justify-content-between">
            <h6>سوال {i+1}</h6>
            {status}{sim_info}
          </div>
          <p>{q['text']}</p>
          <p class="mb-1"><strong>پاسخ دانش‌آموز:</strong> {user_ans_display or '(خالی)'}</p>
          {correct_display}
          <p class="text-muted small mb-0"><strong>توضیح:</strong> {q.get('explanation','')}</p>
        </div>
        """

    content = f"""
    <div class="mb-4">
      <a href="{url_for('exam_results', exam_id=attempt['exam_id'])}" class="btn btn-sm btn-outline-secondary">← بازگشت به نتایج</a>
    </div>
    <div class="card p-4 mb-4">
      <h3>{attempt['student_name']} {attempt['student_family']}</h3>
      <p class="mb-1">تلفن: {attempt['student_phone'] or '-'} | کد ملی: {attempt['student_national_id'] or '-'}</p>
      <p class="mb-1">نمره: <strong>{attempt['score']}/{attempt['max_score']}</strong> ({attempt['percentage']}٪)</p>
      <p class="mb-0 text-muted">زمان: {attempt['time_spent_seconds']//60} دقیقه و {attempt['time_spent_seconds']%60} ثانیه</p>
    </div>
    <h5 class="mb-3">جزئیات پاسخ‌ها</h5>
    {detail_html}
    """
    return render("جزئیات پاسخ", content, user)

# ============================================================
# راه‌اندازی
# ============================================================
if __name__ == "__main__":
    with app.app_context():
        init_db()
    print("=" * 50)
    print("  جامبو آزمون آماده است!")
    print("  اجرا: python exam_maker.py")
    print("  آدرس: http://127.0.0.1:5000")
    print("=" * 50)
    app.run(debug=True, host="0.0.0.0", port=5000)
