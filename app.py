import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = INSTANCE_DIR / "raketex.db"
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("RAKETEX_SECRET_KEY", "dev-change-this-secret")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    INSTANCE_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    UPLOAD_DIR.mkdir(exist_ok=True)
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'build log',
                body TEXT NOT NULL,
                image_filename TEXT,
                published INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def admin_username():
    return os.environ.get("RAKETEX_ADMIN_USER", "admin")


def admin_password_hash():
    password_hash = os.environ.get("RAKETEX_ADMIN_PASSWORD_HASH")
    if password_hash:
        return password_hash
    return generate_password_hash(os.environ.get("RAKETEX_ADMIN_PASSWORD", "raketex123"))


def is_admin():
    return session.get("is_admin") is True


def require_admin():
    if not is_admin():
        flash("Sign in first.", "warn")
        return redirect(url_for("login"))
    return None


def image_is_allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def save_uploaded_image(file_storage):
    if not file_storage or file_storage.filename == "":
        return None
    if not image_is_allowed(file_storage.filename):
        raise ValueError("Use png, jpg, jpeg, gif or webp images.")

    original_name = secure_filename(file_storage.filename)
    suffix = Path(original_name).suffix.lower()
    filename = f"{uuid.uuid4().hex}{suffix}"
    file_storage.save(UPLOAD_DIR / filename)
    return filename


@app.before_request
def ensure_database():
    init_db()


@app.context_processor
def inject_admin_state():
    return {"is_admin": is_admin()}


@app.route("/")
def home():
    search_query = request.args.get("q", "").strip()
    with get_db() as db:
        if search_query:
            like_query = f"%{search_query}%"
            posts = db.execute(
                """
                SELECT * FROM posts
                WHERE published = 1
                  AND (title LIKE ? OR body LIKE ? OR category LIKE ?)
                ORDER BY created_at DESC
                """,
                (like_query, like_query, like_query),
            ).fetchall()
        else:
            posts = db.execute(
                """
                SELECT * FROM posts
                WHERE published = 1
                ORDER BY created_at DESC
                """
            ).fetchall()
    return render_template("home.html", posts=posts, search_query=search_query)


@app.route("/post/<int:post_id>")
def post_detail(post_id):
    with get_db() as db:
        post = db.execute(
            """
            SELECT * FROM posts
            WHERE id = ? AND (published = 1 OR ? = 1)
            """,
            (post_id, 1 if is_admin() else 0),
        ).fetchone()
    if post is None:
        return render_template("404.html"), 404
    return render_template("post.html", post=post)


@app.route("/admin")
def admin():
    blocked = require_admin()
    if blocked:
        return blocked
    with get_db() as db:
        posts = db.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()
    return render_template("admin.html", posts=posts)


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == admin_username() and check_password_hash(admin_password_hash(), password):
            session.clear()
            session["is_admin"] = True
            flash("Signed in.", "ok")
            return redirect(url_for("admin"))
        flash("Wrong username or password.", "warn")
    return render_template("login.html")


@app.route("/admin/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "ok")
    return redirect(url_for("home"))


@app.route("/admin/posts/new", methods=["GET", "POST"])
def new_post():
    blocked = require_admin()
    if blocked:
        return blocked
    if request.method == "POST":
        try:
            image_filename = save_uploaded_image(request.files.get("image"))
        except ValueError as exc:
            flash(str(exc), "warn")
            return render_template("post_form.html", post=None)

        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        category = request.form.get("category", "build log").strip() or "build log"
        published = 1 if request.form.get("published") == "on" else 0

        if not title or not body:
            flash("Title and body are required.", "warn")
            return render_template("post_form.html", post=None)

        timestamp = now_iso()
        with get_db() as db:
            db.execute(
                """
                INSERT INTO posts (title, category, body, image_filename, published, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title, category, body, image_filename, published, timestamp, timestamp),
            )
        flash("Post created.", "ok")
        return redirect(url_for("admin"))
    return render_template("post_form.html", post=None)


@app.route("/admin/posts/<int:post_id>/edit", methods=["GET", "POST"])
def edit_post(post_id):
    blocked = require_admin()
    if blocked:
        return blocked
    with get_db() as db:
        post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        return render_template("404.html"), 404

    if request.method == "POST":
        try:
            new_image = save_uploaded_image(request.files.get("image"))
        except ValueError as exc:
            flash(str(exc), "warn")
            return render_template("post_form.html", post=post)

        image_filename = new_image or post["image_filename"]
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        category = request.form.get("category", "build log").strip() or "build log"
        published = 1 if request.form.get("published") == "on" else 0

        if not title or not body:
            flash("Title and body are required.", "warn")
            return render_template("post_form.html", post=post)

        with get_db() as db:
            db.execute(
                """
                UPDATE posts
                SET title = ?, category = ?, body = ?, image_filename = ?, published = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, category, body, image_filename, published, now_iso(), post_id),
            )
        flash("Post updated.", "ok")
        return redirect(url_for("admin"))

    return render_template("post_form.html", post=post)


@app.route("/admin/posts/<int:post_id>/delete", methods=["POST"])
def delete_post(post_id):
    blocked = require_admin()
    if blocked:
        return blocked
    with get_db() as db:
        db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    flash("Post deleted.", "ok")
    return redirect(url_for("admin"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/assets/<path:filename>")
def asset_file(filename):
    return send_from_directory(BASE_DIR / "assets", filename)


@app.route("/workspace-assets/<path:filename>")
def workspace_asset_file(filename):
    return send_from_directory(BASE_DIR.parent, filename)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
