import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "kredit_v2.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

BUNGA_FLAT = 0.10  # asumsi: bunga flat 10% per tahun
ALLOWED_EXT = {"pdf", "jpg", "jpeg", "png"}

DOC_TYPES = [("ktp", "KTP"), ("kk", "Kartu Keluarga"),
             ("tanda_jadi", "Bukti Bayar Tanda Jadi"),
             ("form_aplikasi", "Form Aplikasi Pengajuan")]

STATUS_LABEL = {
    "DRAFT": ("Draft", "secondary"),
    "MENUNGGU_APPROVAL": ("Menunggu Approval", "warning"),
    "DITOLAK": ("Ditolak", "danger"),
    "DISETUJUI": ("Disetujui", "info"),
    "MENUNGGU_TTD": ("Menunggu TTD Konsumen", "primary"),
    "TTD_SELESAI": ("TTD Selesai", "primary"),
    "DOKUMEN_VALID": ("Dokumen Valid", "info"),
    "DANA_CAIR": ("Dana Cair", "success"),
}
# posisi tahapan pada stepper di halaman detail
STEP_IDX = {"DRAFT": 0, "MENUNGGU_APPROVAL": 1, "DITOLAK": 1, "DISETUJUI": 2,
            "MENUNGGU_TTD": 3, "TTD_SELESAI": 4, "DOKUMEN_VALID": 5, "DANA_CAIR": 6}
# status yang menunggu tindakan tiap role
ACTION = {"sales": [], "marketing": ["DRAFT", "DITOLAK"], "atasan": ["MENUNGGU_APPROVAL"],
          "admin": ["DISETUJUI", "TTD_SELESAI", "DOKUMEN_VALID"]}
ROLE_LABEL = {"sales": "Sales Dealer", "marketing": "Marketing",
              "atasan": "Atasan Marketing", "admin": "Admin Backoffice"}
# notifikasi otomatis saat status berubah: status -> (role penerima, pesan)
NOTIF = {
    "DRAFT": (["marketing"], "Draft baru dari Sales perlu dilengkapi"),
    "MENUNGGU_APPROVAL": (["atasan"], "Pengajuan menunggu approval Anda"),
    "DITOLAK": (["marketing"], "Pengajuan ditolak, mohon direvisi"),
    "DISETUJUI": (["admin"], "Pengajuan disetujui, siap generate kontrak & PO"),
    "MENUNGGU_TTD": (["marketing"], "Link TTD siap dikirim ke konsumen"),
    "TTD_SELESAI": (["admin"], "Konsumen sudah TTD, siap diverifikasi"),
    "DANA_CAIR": (["marketing", "sales"], "Dana sudah dicairkan"),
}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-ganti-di-produksi")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  nama TEXT NOT NULL,
  role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dealer(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nama_dealer TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS konsumen(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nik TEXT UNIQUE NOT NULL,
  nama TEXT NOT NULL, tgl_lahir TEXT NOT NULL, status_kawin TEXT NOT NULL,
  nama_pasangan TEXT, nik_pasangan TEXT);
CREATE TABLE IF NOT EXISTS pengajuan(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  no_pengajuan TEXT,
  konsumen_id INTEGER NOT NULL REFERENCES konsumen(id),
  dealer_id INTEGER NOT NULL REFERENCES dealer(id),
  merk TEXT, model TEXT, tipe TEXT, warna TEXT, harga REAL,
  asuransi REAL, dp REAL, lama INTEGER, angsuran REAL,
  status TEXT, token TEXT, ttd_data TEXT, ttd_waktu TEXT,
  created_by INTEGER REFERENCES users(id), created_at TEXT);
CREATE TABLE IF NOT EXISTS dokumen(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pengajuan_id INTEGER NOT NULL REFERENCES pengajuan(id), jenis TEXT, filename TEXT);
CREATE TABLE IF NOT EXISTS riwayat(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pengajuan_id INTEGER NOT NULL REFERENCES pengajuan(id),
  status TEXT, aktor TEXT, catatan TEXT, waktu TEXT);
CREATE TABLE IF NOT EXISTS notifikasi(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT, pengajuan_id INTEGER, pesan TEXT, waktu TEXT, dibaca INTEGER DEFAULT 0);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    if not db.execute("SELECT 1 FROM users").fetchone():
        for u, p, n, r in [("sales", "sales123", "Sales Dealer", "sales"),
                           ("marketing", "marketing123", "Marketing", "marketing"),
                           ("atasan", "atasan123", "Atasan Marketing", "atasan"),
                           ("admin", "admin123", "Admin Backoffice", "admin")]:
            db.execute("INSERT INTO users(username,password_hash,nama,role) VALUES(?,?,?,?)",
                       (u, generate_password_hash(p), n, r))
        for n in ["Sinar Motor", "Auto Prima", "Mega Motor Sejahtera", "Bintang Otomotif"]:
            db.execute("INSERT INTO dealer(nama_dealer) VALUES(?)", (n,))
    db.commit()
    db.close()


# ---------------------------------------------------------------- helpers
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def hitung_angsuran(harga, asuransi, dp, lama):
    total = (harga - dp + asuransi) * (1 + BUNGA_FLAT * lama / 12)
    return round(total / lama)


def rupiah(v):
    return "Rp " + "{:,.0f}".format(v or 0).replace(",", ".")


app.jinja_env.filters["rp"] = rupiah


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "").encode()
        if not hmac.compare_digest(sent, session.get("csrf", "").encode()):
            abort(400)


def login_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrap(*a, **k):
            if "user" not in session:
                return redirect(url_for("login"))
            if roles and session["user"]["role"] not in roles:
                abort(403)
            return fn(*a, **k)
        return wrap
    return deco


PENGAJUAN_SQL = """SELECT p.*, k.nik, k.nama, k.tgl_lahir, k.status_kawin, k.nama_pasangan,
  k.nik_pasangan, d.nama_dealer AS dealer
  FROM pengajuan p JOIN konsumen k ON k.id=p.konsumen_id JOIN dealer d ON d.id=p.dealer_id"""


def get_pengajuan(pid):
    row = get_db().execute(PENGAJUAN_SQL + " WHERE p.id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    return row


def catat(pid, status, aktor, catatan="", notify=True):
    db = get_db()
    db.execute("INSERT INTO riwayat(pengajuan_id,status,aktor,catatan,waktu) VALUES(?,?,?,?,?)",
               (pid, status, aktor, catatan, now()))
    db.execute("UPDATE pengajuan SET status=? WHERE id=?", (status, pid))
    if notify and status in NOTIF:
        no = db.execute("SELECT no_pengajuan FROM pengajuan WHERE id=?", (pid,)).fetchone()[0]
        roles, pesan = NOTIF[status]
        for r in roles:
            db.execute("INSERT INTO notifikasi(role,pengajuan_id,pesan,waktu) VALUES(?,?,?,?)",
                       (r, pid, "%s: %s" % (no, pesan), now()))


def dokumen_kurang(pid):
    ada = {r["jenis"] for r in get_db().execute(
        "SELECT jenis FROM dokumen WHERE pengajuan_id=?", (pid,))}
    return [lbl for k, lbl in DOC_TYPES if k not in ada]


def jenis_ada(pid):
    return {r["jenis"] for r in get_db().execute(
        "SELECT jenis FROM dokumen WHERE pengajuan_id=?", (pid,))}


def parse_form():
    f = request.form
    d = {k: f.get(k, "").strip() for k in
         ["nama", "nik", "tgl_lahir", "status_kawin", "nama_pasangan", "nik_pasangan",
          "merk", "model", "tipe", "warna"]}
    errors = []
    for k in ["nama", "nik", "tgl_lahir", "status_kawin", "merk", "model", "tipe", "warna"]:
        if not d[k]:
            errors.append("Field %s wajib diisi." % k.replace("_", " "))
    if d["nik"] and not re.fullmatch(r"\d{16}", d["nik"]):
        errors.append("NIK harus 16 digit angka.")
    if d["status_kawin"] == "Kawin":
        if not d["nama_pasangan"] or not re.fullmatch(r"\d{16}", d["nik_pasangan"]):
            errors.append("Data pasangan (nama dan NIK 16 digit) wajib jika status Kawin.")
    else:
        d["nama_pasangan"] = d["nik_pasangan"] = ""
    try:
        d["dealer_id"] = int(f.get("dealer_id", ""))
        if not get_db().execute("SELECT 1 FROM dealer WHERE id=?", (d["dealer_id"],)).fetchone():
            errors.append("Dealer tidak valid.")
    except ValueError:
        d["dealer_id"] = None
        errors.append("Dealer wajib dipilih.")
    try:
        d["harga"] = float(f.get("harga", ""))
        d["asuransi"] = float(f.get("asuransi", "0") or 0)
        d["dp"] = float(f.get("dp", ""))
        d["lama"] = int(f.get("lama", ""))
        if d["harga"] <= 0 or d["dp"] < 0 or d["asuransi"] < 0 or d["lama"] <= 0:
            errors.append("Harga, DP, asuransi, dan lama kredit harus angka valid.")
        elif d["dp"] >= d["harga"]:
            errors.append("Down payment harus lebih kecil dari harga.")
        else:
            d["angsuran"] = hitung_angsuran(d["harga"], d["asuransi"], d["dp"], d["lama"])
    except ValueError:
        errors.append("Harga, DP, asuransi, dan lama kredit harus angka valid.")
    for jenis, lbl in DOC_TYPES:
        fl = request.files.get(jenis)
        if fl and fl.filename:
            ext = fl.filename.rsplit(".", 1)[-1].lower() if "." in fl.filename else ""
            if ext not in ALLOWED_EXT:
                errors.append("%s: format file harus PDF, JPG, atau PNG." % lbl)
    return d, errors


def upsert_konsumen(d):
    db = get_db()
    vals = (d["nama"], d["tgl_lahir"], d["status_kawin"], d["nama_pasangan"], d["nik_pasangan"])
    row = db.execute("SELECT id FROM konsumen WHERE nik=?", (d["nik"],)).fetchone()
    if row:
        db.execute("""UPDATE konsumen SET nama=?,tgl_lahir=?,status_kawin=?,nama_pasangan=?,
                   nik_pasangan=? WHERE id=?""", vals + (row["id"],))
        return row["id"]
    return db.execute("""INSERT INTO konsumen(nama,tgl_lahir,status_kawin,nama_pasangan,
                      nik_pasangan,nik) VALUES(?,?,?,?,?,?)""", vals + (d["nik"],)).lastrowid


def simpan_file(pid):
    db = get_db()
    for jenis, _ in DOC_TYPES:
        fl = request.files.get(jenis)
        if fl and fl.filename:
            name = "%s_%s_%s" % (pid, jenis, secure_filename(fl.filename))
            fl.save(os.path.join(UPLOAD_DIR, name))
            db.execute("DELETE FROM dokumen WHERE pengajuan_id=? AND jenis=?", (pid, jenis))
            db.execute("INSERT INTO dokumen(pengajuan_id,jenis,filename) VALUES(?,?,?)",
                       (pid, jenis, name))


def boleh_edit(p, role):
    return ((role == "marketing" and p["status"] in ("DRAFT", "DITOLAK")) or
            (role == "sales" and p["status"] == "DRAFT"))


def get_dealers():
    return get_db().execute("SELECT * FROM dealer ORDER BY nama_dealer").fetchall()


@app.context_processor
def inject():
    u = session.get("user")
    unread = 0
    if u:
        unread = get_db().execute("SELECT COUNT(*) FROM notifikasi WHERE role=? AND dibaca=0",
                                  (u["role"],)).fetchone()[0]
    return dict(status_label=STATUS_LABEL, doc_types=DOC_TYPES, user=u, unread=unread,
                bunga=BUNGA_FLAT, role_label=ROLE_LABEL)


ERR_MSG = {400: "Permintaan tidak valid atau sesi kedaluwarsa. Silakan muat ulang halaman.",
           403: "Anda tidak memiliki akses ke halaman ini.",
           404: "Halaman atau data tidak ditemukan.",
           413: "Ukuran file terlalu besar (maksimal 20 MB)."}


def error_page(e):
    return render_template("error.html", code=e.code, msg=ERR_MSG.get(e.code, "Terjadi kesalahan.")), e.code


for _c in ERR_MSG:
    app.register_error_handler(_c, error_page)


# ---------------------------------------------------------------- auth
@app.route("/")
def index():
    return redirect(url_for("dashboard" if "user" in session else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = get_db().execute("SELECT * FROM users WHERE username=?",
                             (request.form.get("username", "").strip(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session["user"] = {"id": u["id"], "nama": u["nama"], "role": u["role"]}
            return redirect(url_for("dashboard"))
        flash("Username atau password salah.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- halaman utama
@app.route("/dashboard")
@login_required()
def dashboard():
    db = get_db()
    counts = {r["status"]: r["c"] for r in db.execute(
        "SELECT status, COUNT(*) c FROM pengajuan GROUP BY status")}
    role = session["user"]["role"]
    perlu = sum(counts.get(s, 0) for s in ACTION[role])
    terbaru = db.execute(PENGAJUAN_SQL + " ORDER BY p.id DESC LIMIT 5").fetchall()
    return render_template("dashboard.html", counts=counts, perlu=perlu, terbaru=terbaru,
                           total=sum(counts.values()), aksi_status=ACTION[role])


@app.route("/pengajuan")
@login_required()
def daftar():
    q = request.args.get("q", "").strip()
    st = request.args.get("status", "")
    sql, args = PENGAJUAN_SQL + " WHERE 1=1", []
    if q:
        sql += " AND (k.nama LIKE ? OR p.no_pengajuan LIKE ? OR k.nik LIKE ?)"
        args += ["%" + q + "%"] * 3
    if st in STATUS_LABEL:
        sql += " AND p.status=?"
        args.append(st)
    rows = get_db().execute(sql + " ORDER BY p.id DESC", args).fetchall()
    return render_template("list.html", rows=rows, q=q, st=st)


@app.route("/notifikasi")
@login_required()
def notifikasi():
    db = get_db()
    role = session["user"]["role"]
    rows = db.execute("SELECT * FROM notifikasi WHERE role=? ORDER BY id DESC LIMIT 50", (role,)).fetchall()
    db.execute("UPDATE notifikasi SET dibaca=1 WHERE role=?", (role,))
    db.commit()
    return render_template("notif.html", rows=rows)


# ---------------------------------------------------------------- pengajuan
def simpan_dan_tentukan_status(pid, role, edit):
    """Tentukan status setelah data disimpan. Marketing hanya bisa submit jika dokumen lengkap."""
    aktor = session["user"]["nama"]
    if role == "marketing":
        kurang = dokumen_kurang(pid)
        if not kurang:
            catat(pid, "MENUNGGU_APPROVAL", aktor, "Data lengkap dan disubmit")
            return None
        catat(pid, "DRAFT", aktor, "Disimpan sebagai draft, dokumen belum lengkap", notify=False)
        return kurang
    catat(pid, "DRAFT", aktor, "Draft diperbarui" if edit else "Pengajuan awal dibuat", notify=not edit)
    return None


@app.route("/pengajuan/baru", methods=["GET", "POST"])
@login_required("sales", "marketing")
def baru():
    d, errors = {}, []
    if request.method == "POST":
        d, errors = parse_form()
        if not errors:
            db = get_db()
            kid = upsert_konsumen(d)
            pid = db.execute(
                """INSERT INTO pengajuan(konsumen_id,dealer_id,merk,model,tipe,warna,harga,asuransi,
                dp,lama,angsuran,status,created_by,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'DRAFT',?,?)""",
                (kid, d["dealer_id"], d["merk"], d["model"], d["tipe"], d["warna"], d["harga"],
                 d["asuransi"], d["dp"], d["lama"], d["angsuran"], session["user"]["id"], now())).lastrowid
            db.execute("UPDATE pengajuan SET no_pengajuan=? WHERE id=?",
                       ("PK-%s-%04d" % (datetime.now().year, pid), pid))
            simpan_file(pid)
            kurang = simpan_dan_tentukan_status(pid, session["user"]["role"], False)
            db.commit()
            if kurang:
                flash("Tersimpan sebagai Draft. Dokumen belum lengkap: " + ", ".join(kurang), "warning")
            else:
                flash("Pengajuan berhasil disimpan.", "success")
            return redirect(url_for("detail", pid=pid))
    return render_template("form.html", d=d, errors=errors, edit=False,
                           dealers=get_dealers(), have=set())


@app.route("/pengajuan/<int:pid>/edit", methods=["GET", "POST"])
@login_required("sales", "marketing")
def edit(pid):
    p = get_pengajuan(pid)
    if not boleh_edit(p, session["user"]["role"]):
        abort(403)
    d, errors = dict(p), []
    if request.method == "POST":
        d, errors = parse_form()
        if not errors:
            db = get_db()
            kid = upsert_konsumen(d)
            db.execute("""UPDATE pengajuan SET konsumen_id=?,dealer_id=?,merk=?,model=?,tipe=?,warna=?,
                       harga=?,asuransi=?,dp=?,lama=?,angsuran=? WHERE id=?""",
                       (kid, d["dealer_id"], d["merk"], d["model"], d["tipe"], d["warna"], d["harga"],
                        d["asuransi"], d["dp"], d["lama"], d["angsuran"], pid))
            simpan_file(pid)
            kurang = simpan_dan_tentukan_status(pid, session["user"]["role"], True)
            db.commit()
            if kurang:
                flash("Tersimpan sebagai Draft. Dokumen belum lengkap: " + ", ".join(kurang), "warning")
            else:
                flash("Pengajuan diperbarui.", "success")
            return redirect(url_for("detail", pid=pid))
    return render_template("form.html", d=d, errors=errors, edit=True,
                           dealers=get_dealers(), have=jenis_ada(pid))


@app.route("/pengajuan/<int:pid>")
@login_required()
def detail(pid):
    p = get_pengajuan(pid)
    db = get_db()
    dokumen = db.execute("SELECT * FROM dokumen WHERE pengajuan_id=?", (pid,)).fetchall()
    riwayat = db.execute("SELECT * FROM riwayat WHERE pengajuan_id=? ORDER BY id", (pid,)).fetchall()
    return render_template("detail.html", p=p, dokumen=dokumen, riwayat=riwayat,
                           bisa_edit=boleh_edit(p, session["user"]["role"]),
                           step_idx=STEP_IDX[p["status"]], have=jenis_ada(pid))


@app.route("/pengajuan/<int:pid>/keputusan", methods=["POST"])
@login_required("atasan")
def keputusan(pid):
    p = get_pengajuan(pid)
    if p["status"] != "MENUNGGU_APPROVAL":
        abort(400)
    aksi = request.form.get("aksi")
    catatan = request.form.get("catatan", "").strip()
    if aksi == "reject":
        if not catatan:
            flash("Catatan wajib diisi saat menolak pengajuan.", "danger")
            return redirect(url_for("detail", pid=pid))
        catat(pid, "DITOLAK", session["user"]["nama"], catatan)
    elif aksi == "approve":
        catat(pid, "DISETUJUI", session["user"]["nama"], catatan or "Disetujui")
    else:
        abort(400)
    get_db().commit()
    return redirect(url_for("detail", pid=pid))


@app.route("/pengajuan/<int:pid>/generate", methods=["POST"])
@login_required("admin")
def generate(pid):
    p = get_pengajuan(pid)
    if p["status"] != "DISETUJUI":
        abort(400)
    db = get_db()
    db.execute("UPDATE pengajuan SET token=? WHERE id=?", (secrets.token_urlsafe(16), pid))
    catat(pid, "MENUNGGU_TTD", session["user"]["nama"], "Kontrak dan PO digenerate, link TTD dibuat")
    db.commit()
    return redirect(url_for("detail", pid=pid))


@app.route("/pengajuan/<int:pid>/verifikasi", methods=["POST"])
@login_required("admin")
def verifikasi(pid):
    p = get_pengajuan(pid)
    if p["status"] != "TTD_SELESAI":
        abort(400)
    catat(pid, "DOKUMEN_VALID", session["user"]["nama"], "Kontrak ber-TTD diverifikasi valid")
    get_db().commit()
    return redirect(url_for("detail", pid=pid))


@app.route("/pengajuan/<int:pid>/cair", methods=["POST"])
@login_required("admin")
def cair(pid):
    p = get_pengajuan(pid)
    if p["status"] != "DOKUMEN_VALID":
        abort(400)
    catat(pid, "DANA_CAIR", session["user"]["nama"], "Dana dicairkan ke dealer")
    get_db().commit()
    return redirect(url_for("detail", pid=pid))


@app.route("/kontrak/<int:pid>")
@login_required()
def kontrak(pid):
    p = get_pengajuan(pid)
    if not p["token"]:
        abort(404)
    return render_template("doc.html", p=p, jenis="kontrak")


@app.route("/uploads/<path:name>")
@login_required()
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name)


# ------------------------------------------ halaman via link (tanpa login)
def by_token(token):
    p = get_db().execute(PENGAJUAN_SQL + " WHERE p.token=?", (token,)).fetchone()
    if not p:
        abort(404)
    return p


@app.route("/po/<token>")
def po(token):
    return render_template("doc.html", p=by_token(token), jenis="po")


@app.route("/ttd/<token>", methods=["GET", "POST"])
def ttd(token):
    p = by_token(token)
    if request.method == "POST":
        data = request.form.get("ttd_data", "")
        if p["status"] != "MENUNGGU_TTD":
            flash("Kontrak ini tidak menunggu tanda tangan.", "warning")
        elif not data.startswith("data:image/png;base64,") or len(data) > 500000:
            flash("Tanda tangan tidak valid.", "danger")
        else:
            db = get_db()
            db.execute("UPDATE pengajuan SET ttd_data=?, ttd_waktu=? WHERE id=?", (data, now(), p["id"]))
            catat(p["id"], "TTD_SELESAI", "Konsumen: " + p["nama"], "Kontrak ditandatangani digital")
            db.commit()
        return redirect(url_for("ttd", token=token))
    return render_template("sign.html", p=p)


# ---------------------------------------------------------------- templates
TEMPLATES = {
"base.html": """<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kredit Kendaraan - PT. JKL</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{--brand:#0b5ea8}
body{background:#f4f6f9}
.navbar{background:var(--brand)}
.card{border:0;box-shadow:0 1px 3px rgba(0,0,0,.08);border-radius:10px}
.btn-primary{background:var(--brand);border-color:var(--brand)}
h6.sec{color:var(--brand);border-bottom:2px solid #e3ecf6;padding-bottom:.4rem;margin-top:.5rem}
.stat{border-left:4px solid var(--brand)}
.stepper{display:flex;margin:.5rem 0 1.5rem}
.step{flex:1;text-align:center;position:relative;font-size:.8rem;color:#6c757d}
.step:not(:first-child)::before{content:"";position:absolute;top:15px;left:-50%;width:100%;height:3px;background:#dee2e6}
.step.done:not(:first-child)::before,.step.active:not(:first-child)::before{background:#198754}
.dot{width:32px;height:32px;line-height:32px;border-radius:50%;background:#dee2e6;margin:0 auto 4px;position:relative;z-index:1;font-weight:600}
.step.done .dot{background:#198754;color:#fff}
.step.active .dot{background:var(--brand);color:#fff}
.step.rejected .dot{background:#dc3545;color:#fff}
.step.done .lbl,.step.active .lbl{color:#212529;font-weight:600}
@media print{.navbar,.d-print-none{display:none!important}body{background:#fff}}
</style></head>
<body>
<nav class="navbar navbar-expand navbar-dark mb-4"><div class="container">
<a class="navbar-brand fw-semibold" href="{{ url_for('index') }}">PT. JKL Kredit Kendaraan</a>
{% if user %}<div class="navbar-nav me-auto">
<a class="nav-link" href="{{ url_for('dashboard') }}">Dashboard</a>
<a class="nav-link" href="{{ url_for('daftar') }}">Pengajuan</a>
<a class="nav-link" href="{{ url_for('notifikasi') }}">Notifikasi
{% if unread %}<span class="badge text-bg-danger">{{ unread }}</span>{% endif %}</a></div>
<span class="text-white small">{{ user.nama }}
<a class="btn btn-sm btn-outline-light ms-2" href="{{ url_for('logout') }}">Logout</a></span>{% endif %}
</div></nav>
<div class="container pb-5">
{% for cat, msg in get_flashed_messages(with_categories=true) %}
<div class="alert alert-{{ cat }} alert-dismissible fade show">{{ msg }}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>{% endfor %}
{% block body %}{% endblock %}</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>""",

"error.html": """{% extends "base.html" %}{% block body %}
<div class="text-center py-5"><h1 class="display-4">{{ code }}</h1><p class="lead">{{ msg }}</p>
<a class="btn btn-primary" href="{{ url_for('index') }}">Kembali ke beranda</a></div>{% endblock %}""",

"login.html": """{% extends "base.html" %}{% block body %}
<div class="row justify-content-center"><div class="col-md-5"><div class="card"><div class="card-body p-4">
<h5 class="mb-3">Login</h5>
<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<div class="mb-3"><label class="form-label">Username</label><input name="username" class="form-control" required autofocus></div>
<div class="mb-3"><label class="form-label">Password</label><input type="password" name="password" class="form-control" required></div>
<button class="btn btn-primary w-100">Masuk</button></form>
<hr><div class="small text-muted">Akun demo: sales/sales123, marketing/marketing123,
atasan/atasan123, admin/admin123</div>
</div></div></div></div>{% endblock %}""",

"dashboard.html": """{% extends "base.html" %}{% block body %}
<h4 class="mb-3">Halo, {{ user.nama }}</h4>
<div class="row g-3 mb-4">
<div class="col-md-4"><div class="card stat card-body"><div class="text-muted small">Total pengajuan</div><div class="fs-2 fw-semibold">{{ total }}</div></div></div>
{% if aksi_status %}<div class="col-md-4"><a class="text-decoration-none" href="{{ url_for('daftar', status=aksi_status[0]) }}">
<div class="card stat card-body" style="border-left-color:#f0ad4e"><div class="text-muted small">Perlu tindakan Anda</div><div class="fs-2 fw-semibold text-dark">{{ perlu }}</div></div></a></div>
{% else %}<div class="col-md-4"><div class="card stat card-body"><div class="text-muted small">Tugas Anda</div>
<a class="btn btn-primary btn-sm mt-2" href="{{ url_for('baru') }}">+ Buat pengajuan</a></div></div>{% endif %}
<div class="col-md-4"><div class="card stat card-body" style="border-left-color:#198754"><div class="text-muted small">Dana cair</div><div class="fs-2 fw-semibold">{{ counts.get('DANA_CAIR', 0) }}</div></div></div>
</div>
<div class="card card-body mb-4"><h6 class="sec">Pengajuan per status</h6>
<div class="d-flex flex-wrap gap-2">{% for k, v in status_label.items() %}
<a class="text-decoration-none" href="{{ url_for('daftar', status=k) }}"><span class="badge text-bg-{{ v[1] }} p-2">{{ v[0] }}: {{ counts.get(k, 0) }}</span></a>{% endfor %}</div></div>
<div class="card card-body"><h6 class="sec">Pengajuan terbaru</h6>
<table class="table table-hover align-middle mb-0"><tbody>
{% for r in terbaru %}<tr><td>{{ r.no_pengajuan }}</td><td>{{ r.nama }}</td><td>{{ r.merk }} {{ r.model }}</td>
<td><span class="badge text-bg-{{ status_label[r.status][1] }}">{{ status_label[r.status][0] }}</span></td>
<td class="text-end"><a class="btn btn-sm btn-outline-primary" href="{{ url_for('detail', pid=r.id) }}">Detail</a></td></tr>
{% else %}<tr><td class="text-muted">Belum ada pengajuan.</td></tr>{% endfor %}</tbody></table></div>
{% endblock %}""",

"list.html": """{% extends "base.html" %}{% block body %}
<div class="d-flex justify-content-between mb-3"><h4>Daftar Pengajuan</h4>
{% if user.role in ('sales','marketing') %}<a class="btn btn-primary" href="{{ url_for('baru') }}">+ Pengajuan Baru</a>{% endif %}</div>
<form class="row g-2 mb-3" method="get">
<div class="col-md-5"><input name="q" value="{{ q }}" class="form-control" placeholder="Cari nama, NIK, atau no pengajuan"></div>
<div class="col-md-4"><select name="status" class="form-select"><option value="">Semua status</option>
{% for k, v in status_label.items() %}<option value="{{ k }}" {{ 'selected' if st==k }}>{{ v[0] }}</option>{% endfor %}</select></div>
<div class="col-md-3"><button class="btn btn-outline-primary">Filter</button>
<a class="btn btn-link" href="{{ url_for('daftar') }}">Reset</a></div></form>
<div class="card"><div class="table-responsive"><table class="table table-hover align-middle mb-0">
<thead><tr><th>No</th><th>Konsumen</th><th>Kendaraan</th><th>Harga</th><th>Angsuran</th><th>Status</th><th></th></tr></thead><tbody>
{% for r in rows %}<tr><td>{{ r.no_pengajuan }}</td><td>{{ r.nama }}</td><td>{{ r.merk }} {{ r.model }}</td>
<td>{{ r.harga|rp }}</td><td>{{ r.angsuran|rp }}/bln</td>
<td><span class="badge text-bg-{{ status_label[r.status][1] }}">{{ status_label[r.status][0] }}</span></td>
<td><a class="btn btn-sm btn-outline-primary" href="{{ url_for('detail', pid=r.id) }}">Detail</a></td></tr>
{% else %}<tr><td colspan="7" class="text-center text-muted py-4">Tidak ada data.</td></tr>{% endfor %}
</tbody></table></div></div>{% endblock %}""",

"notif.html": """{% extends "base.html" %}{% block body %}
<h4 class="mb-3">Notifikasi</h4>
<div class="card"><ul class="list-group list-group-flush">
{% for r in rows %}<li class="list-group-item d-flex justify-content-between">
<a class="text-decoration-none" href="{{ url_for('detail', pid=r.pengajuan_id) }}">{{ r.pesan }}</a>
<small class="text-muted">{{ r.waktu }}</small></li>
{% else %}<li class="list-group-item text-muted">Belum ada notifikasi.</li>{% endfor %}</ul></div>{% endblock %}""",

"form.html": """{% extends "base.html" %}{% block body %}
<h4 class="mb-3">{{ 'Lengkapi / Ubah' if edit else 'Input' }} Pengajuan Kredit</h4>
{% for e in errors %}<div class="alert alert-danger py-2">{{ e }}</div>{% endfor %}
<form method="post" enctype="multipart/form-data" class="card card-body row g-3 mx-0">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<h6 class="sec">Data Konsumen</h6>
<div class="col-md-6"><label class="form-label">Nama</label><input name="nama" class="form-control" value="{{ d.get('nama','') }}" required></div>
<div class="col-md-6"><label class="form-label">NIK (16 digit)</label><input name="nik" maxlength="16" inputmode="numeric" class="form-control" value="{{ d.get('nik','') }}" required></div>
<div class="col-md-4"><label class="form-label">Tanggal Lahir</label><input type="date" name="tgl_lahir" class="form-control" value="{{ d.get('tgl_lahir','') }}" required></div>
<div class="col-md-4"><label class="form-label">Status Perkawinan</label>
<select name="status_kawin" id="sk" class="form-select" onchange="togglePasangan()">
{% for s in ['Belum Kawin','Kawin','Cerai'] %}<option {{ 'selected' if d.get('status_kawin')==s }}>{{ s }}</option>{% endfor %}</select></div>
<div id="pasangan" class="row g-3 mx-0 px-0">
<div class="col-md-6"><label class="form-label">Nama Pasangan</label><input name="nama_pasangan" class="form-control" value="{{ d.get('nama_pasangan','') }}"></div>
<div class="col-md-6"><label class="form-label">NIK Pasangan</label><input name="nik_pasangan" maxlength="16" inputmode="numeric" class="form-control" value="{{ d.get('nik_pasangan','') }}"></div></div>
<h6 class="sec mt-3">Data Kendaraan</h6>
<div class="col-md-4"><label class="form-label">Dealer</label><select name="dealer_id" class="form-select" required>
<option value="">Pilih dealer</option>{% for dl in dealers %}<option value="{{ dl.id }}" {{ 'selected' if d.get('dealer_id')==dl.id }}>{{ dl.nama_dealer }}</option>{% endfor %}</select></div>
<div class="col-md-4"><label class="form-label">Merk</label><input name="merk" class="form-control" value="{{ d.get('merk','') }}" required></div>
<div class="col-md-4"><label class="form-label">Model</label><input name="model" class="form-control" value="{{ d.get('model','') }}" required></div>
<div class="col-md-4"><label class="form-label">Tipe</label><input name="tipe" class="form-control" value="{{ d.get('tipe','') }}" required></div>
<div class="col-md-4"><label class="form-label">Warna</label><input name="warna" class="form-control" value="{{ d.get('warna','') }}" required></div>
<div class="col-md-4"><label class="form-label">Harga (Rp)</label><input type="number" id="harga" name="harga" min="1" class="form-control" value="{{ d.get('harga','') }}" oninput="hitung()" required></div>
<h6 class="sec mt-3">Data Pinjaman</h6>
<div class="col-md-3"><label class="form-label">Asuransi (Rp)</label><input type="number" id="asuransi" name="asuransi" min="0" class="form-control" value="{{ d.get('asuransi', 0) }}" oninput="hitung()"></div>
<div class="col-md-3"><label class="form-label">Down Payment (Rp)</label><input type="number" id="dp" name="dp" min="0" class="form-control" value="{{ d.get('dp','') }}" oninput="hitung()" required></div>
<div class="col-md-3"><label class="form-label">Lama Kredit (bulan)</label><input type="number" id="lama" name="lama" min="1" class="form-control" value="{{ d.get('lama','') }}" oninput="hitung()" required></div>
<div class="col-md-3"><label class="form-label">Angsuran / Bulan</label><input id="angsuran" class="form-control fw-semibold" readonly></div>
<div class="form-text">Angsuran dihitung otomatis: bunga flat {{ (bunga*100)|int }}% per tahun.</div>
<h6 class="sec mt-3">Dokumen Pendukung (PDF / JPG / PNG)</h6>
{% for k, label in doc_types %}<div class="col-md-6"><label class="form-label">{{ label }}
{% if k in have %}<span class="badge text-bg-success">sudah diupload</span>{% endif %}</label>
<input type="file" name="{{ k }}" class="form-control" accept=".pdf,.jpg,.jpeg,.png"></div>{% endfor %}
<div class="form-text">Marketing hanya dapat submit ke approval jika keempat dokumen sudah lengkap. Jika belum, data disimpan sebagai Draft.</div>
<div class="col-12"><button class="btn btn-primary">Simpan</button>
<a class="btn btn-link" href="{{ url_for('daftar') }}">Batal</a></div>
</form>
<script>
const B = {{ bunga }};
function hitung(){
  const h=+harga.value||0, a=+asuransi.value||0, d=+dp.value||0, l=+lama.value||0;
  angsuran.value = (h>d && l>0) ? 'Rp ' + Math.round(((h-d+a)*(1+B*l/12))/l).toLocaleString('id-ID') : '';
}
function togglePasangan(){ pasangan.style.display = sk.value==='Kawin' ? '' : 'none'; }
hitung(); togglePasangan();
</script>{% endblock %}""",

"detail.html": """{% extends "base.html" %}{% block body %}
<div class="d-flex justify-content-between align-items-center mb-2">
<h4 class="mb-0">{{ p.no_pengajuan }}
<span class="badge text-bg-{{ status_label[p.status][1] }}">{{ status_label[p.status][0] }}</span></h4>
<a class="btn btn-outline-secondary" href="{{ url_for('daftar') }}">Kembali</a></div>
{% set steps = ['Input data','Approval','Kontrak & PO','TTD Konsumen','Verifikasi','Pencairan'] %}
<div class="stepper">{% for s in steps %}
<div class="step {{ 'done' if loop.index0 < step_idx else ('active' if loop.index0 == step_idx else '') }}{{ ' rejected' if p.status=='DITOLAK' and loop.index0==step_idx else '' }}">
<div class="dot">{{ '&#10003;'|safe if loop.index0 < step_idx else loop.index }}</div><div class="lbl">{{ s }}</div></div>{% endfor %}</div>
<div class="row g-3">
<div class="col-md-8"><div class="card card-body">
<h6 class="sec">Data Konsumen</h6>
<table class="table table-sm"><tr><th width="35%">Nama</th><td>{{ p.nama }}</td></tr>
<tr><th>NIK</th><td>{{ p.nik }}</td></tr><tr><th>Tanggal Lahir</th><td>{{ p.tgl_lahir }}</td></tr>
<tr><th>Status Perkawinan</th><td>{{ p.status_kawin }}</td></tr>
{% if p.nama_pasangan %}<tr><th>Pasangan</th><td>{{ p.nama_pasangan }} ({{ p.nik_pasangan }})</td></tr>{% endif %}</table>
<h6 class="sec">Data Kendaraan</h6>
<table class="table table-sm"><tr><th width="35%">Dealer</th><td>{{ p.dealer }}</td></tr>
<tr><th>Kendaraan</th><td>{{ p.merk }} {{ p.model }} {{ p.tipe }} - {{ p.warna }}</td></tr>
<tr><th>Harga</th><td>{{ p.harga|rp }}</td></tr></table>
<h6 class="sec">Data Pinjaman</h6>
<table class="table table-sm"><tr><th width="35%">Asuransi</th><td>{{ p.asuransi|rp }}</td></tr>
<tr><th>Down Payment</th><td>{{ p.dp|rp }}</td></tr><tr><th>Lama Kredit</th><td>{{ p.lama }} bulan</td></tr>
<tr><th>Angsuran / Bulan</th><td class="fw-semibold">{{ p.angsuran|rp }}</td></tr></table>
<h6 class="sec">Kelengkapan Dokumen</h6>
<ul class="list-unstyled mb-0">{% for k, label in doc_types %}<li>
{% if k in have %}<span class="text-success">&#10003;</span> {% for d in dokumen if d.jenis == k %}<a href="{{ url_for('uploads', name=d.filename) }}" target="_blank">{{ label }}</a>{% endfor %}
{% else %}<span class="text-danger">&#10007;</span> {{ label }} <small class="text-muted">(belum ada)</small>{% endif %}</li>{% endfor %}</ul>
</div></div>
<div class="col-md-4">
<div class="card card-body mb-3"><h6 class="sec">Aksi</h6>
{% if bisa_edit %}<a class="btn btn-warning mb-2" href="{{ url_for('edit', pid=p.id) }}">{{ 'Lengkapi / Submit' if user.role=='marketing' else 'Ubah Draft' }}</a>{% endif %}
{% if user.role=='atasan' and p.status=='MENUNGGU_APPROVAL' %}
<form method="post" action="{{ url_for('keputusan', pid=p.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<textarea name="catatan" class="form-control mb-2" placeholder="Catatan (wajib jika ditolak)"></textarea>
<button name="aksi" value="approve" class="btn btn-success">Approve</button>
<button name="aksi" value="reject" class="btn btn-danger">Reject</button></form>{% endif %}
{% if user.role=='admin' and p.status=='DISETUJUI' %}
<form method="post" action="{{ url_for('generate', pid=p.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn btn-primary">Generate Kontrak &amp; PO</button></form>{% endif %}
{% if user.role=='admin' and p.status=='TTD_SELESAI' %}
<form method="post" action="{{ url_for('verifikasi', pid=p.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn btn-primary">Verifikasi Dokumen TTD</button></form>{% endif %}
{% if user.role=='admin' and p.status=='DOKUMEN_VALID' %}
<form method="post" action="{{ url_for('cair', pid=p.id) }}" onsubmit="return confirm('Cairkan dana untuk pengajuan ini?')"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn btn-success">Cairkan Dana</button></form>{% endif %}
{% if p.token %}
<a class="btn btn-outline-primary mt-2" target="_blank" href="{{ url_for('kontrak', pid=p.id) }}">Lihat Kontrak</a>
<a class="btn btn-outline-primary mt-2" target="_blank" href="{{ url_for('po', token=p.token) }}">Lihat PO (link Dealer)</a>
<div class="small mt-2">Link TTD Konsumen:<br><a target="_blank" href="{{ url_for('ttd', token=p.token) }}">{{ request.host_url }}ttd/{{ p.token }}</a></div>{% endif %}
</div>
<div class="card card-body"><h6 class="sec">Riwayat Status</h6>
<ul class="list-unstyled small mb-0">{% for r in riwayat %}
<li class="mb-2"><b>{{ status_label[r.status][0] }}</b><br>{{ r.aktor }} - {{ r.waktu }}
{% if r.catatan %}<br><i>{{ r.catatan }}</i>{% endif %}</li>{% endfor %}</ul></div>
</div></div>{% endblock %}""",

"kontrak_body.html": """<h4 class="text-center">PERJANJIAN PEMBIAYAAN KENDARAAN BERMOTOR</h4>
<p class="text-center text-muted">No: {{ p.no_pengajuan }}</p>
<p>Perjanjian ini dibuat antara <b>PT. JKL</b> (Pemberi Pembiayaan) dan <b>{{ p.nama }}</b> (NIK {{ p.nik }}) (Konsumen).</p>
<table class="table table-sm">
<tr><th width="35%">Kendaraan</th><td>{{ p.merk }} {{ p.model }} {{ p.tipe }} - {{ p.warna }}</td></tr>
<tr><th>Dealer</th><td>{{ p.dealer }}</td></tr>
<tr><th>Harga Kendaraan</th><td>{{ p.harga|rp }}</td></tr>
<tr><th>Down Payment</th><td>{{ p.dp|rp }}</td></tr>
<tr><th>Asuransi</th><td>{{ p.asuransi|rp }}</td></tr>
<tr><th>Lama Kredit</th><td>{{ p.lama }} bulan</td></tr>
<tr><th>Angsuran / Bulan</th><td>{{ p.angsuran|rp }}</td></tr></table>
<p>Konsumen menyatakan setuju atas seluruh ketentuan pembiayaan di atas.</p>""",

"doc.html": """{% extends "base.html" %}{% block body %}
<div class="card card-body">
{% if jenis == 'kontrak' %}
{% include "kontrak_body.html" %}
<div class="mt-4">Konsumen,<br>
{% if p.ttd_data %}<img src="{{ p.ttd_data }}" style="height:90px"><br>{% else %}<br><br><br>{% endif %}
<b>{{ p.nama }}</b>{% if p.ttd_waktu %}<br><small class="text-muted">Ditandatangani digital: {{ p.ttd_waktu }}</small>{% endif %}</div>
{% else %}
<h4 class="text-center">PURCHASE ORDER (PO)</h4>
<p class="text-center text-muted">No: PO-{{ p.no_pengajuan }}</p>
<p>Kepada: <b>{{ p.dealer }}</b><br>Mohon disiapkan kendaraan berikut untuk konsumen <b>{{ p.nama }}</b>:</p>
<table class="table table-sm">
<tr><th width="35%">Kendaraan</th><td>{{ p.merk }} {{ p.model }} {{ p.tipe }}</td></tr>
<tr><th>Warna</th><td>{{ p.warna }}</td></tr>
<tr><th>Harga</th><td>{{ p.harga|rp }}</td></tr>
<tr><th>Pembayaran dari PT. JKL</th><td>{{ (p.harga - p.dp)|rp }}</td></tr></table>
<p>Hormat kami,<br><b>PT. JKL</b></p>
{% endif %}
<button class="btn btn-outline-secondary mt-3 d-print-none" onclick="window.print()">Cetak / Simpan PDF</button>
</div>{% endblock %}""",

"sign.html": """{% extends "base.html" %}{% block body %}
<div class="card card-body">
{% include "kontrak_body.html" %}
<hr>
{% if p.status == 'MENUNGGU_TTD' %}
<h6>Tanda Tangan Digital</h6>
<p class="small text-muted">Tanda tangan pada kotak di bawah menggunakan mouse atau jari.</p>
<canvas id="pad" width="500" height="180" style="border:1px solid #999;background:#fff;touch-action:none;max-width:100%"></canvas>
<form method="post" class="mt-2" onsubmit="return kirim()">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
<input type="hidden" name="ttd_data" id="ttd_data">
<button type="button" class="btn btn-outline-secondary" onclick="hapus()">Hapus</button>
<button class="btn btn-primary">Tandatangani Kontrak</button></form>
<script>
const c=document.getElementById('pad'), x=c.getContext('2d'); let draw=false, ada=false;
x.lineWidth=2; x.lineCap='round';
function pos(e){const r=c.getBoundingClientRect();return [(e.clientX-r.left)*c.width/r.width,(e.clientY-r.top)*c.height/r.height];}
c.addEventListener('pointerdown',e=>{draw=true;ada=true;const [a,b]=pos(e);x.beginPath();x.moveTo(a,b);});
c.addEventListener('pointermove',e=>{if(!draw)return;const [a,b]=pos(e);x.lineTo(a,b);x.stroke();});
window.addEventListener('pointerup',()=>draw=false);
function hapus(){x.clearRect(0,0,c.width,c.height);ada=false;}
function kirim(){if(!ada){alert('Silakan tanda tangan terlebih dahulu.');return false;}
document.getElementById('ttd_data').value=c.toDataURL('image/png');return true;}
</script>
{% elif p.ttd_data %}
<div class="alert alert-success">Kontrak sudah ditandatangani pada {{ p.ttd_waktu }}. Terima kasih.</div>
<img src="{{ p.ttd_data }}" style="height:90px">
{% endif %}
</div>{% endblock %}""",
}

app.jinja_loader = DictLoader(TEMPLATES)

init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
