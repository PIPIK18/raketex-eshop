import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    Response,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("RAKETEX_DATA_DIR", BASE_DIR / "instance"))
INSTANCE_DIR = DATA_DIR
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = INSTANCE_DIR / "raketex.db"
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
BLOB_READ_WRITE_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN")
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
DB_INIT_DONE = False
PRIVATE_BLOB_PREFIX = "blob-private:"
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI")


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("RAKETEX_SECRET_KEY", "dev-change-this-secret")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def using_postgres():
    return bool(DATABASE_URL)


def running_on_vercel():
    return bool(os.environ.get("VERCEL"))


def storage_config_issues():
    issues = []
    if running_on_vercel() and not using_postgres():
        issues.append("Missing DATABASE_URL or POSTGRES_URL. Connect a Vercel Marketplace Postgres database.")
    if running_on_vercel() and not using_blob_storage():
        issues.append("Missing BLOB_READ_WRITE_TOKEN. Connect a Vercel Blob store.")
    return issues


@contextmanager
def get_db():
    if using_postgres():
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            yield conn
        return

    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        yield conn


def init_db():
    global DB_INIT_DONE
    if DB_INIT_DONE:
        return

    if not using_blob_storage():
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with get_db() as db:
        if using_postgres():
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'build log',
                    body TEXT NOT NULL,
                    image_filename TEXT,
                    published BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    google_id TEXT UNIQUE NOT NULL,
                    email TEXT,
                    display_name TEXT NOT NULL,
                    avatar_url TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS comments (
                    id BIGSERIAL PRIMARY KEY,
                    post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
        else:
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
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    google_id TEXT UNIQUE NOT NULL,
                    email TEXT,
                    display_name TEXT NOT NULL,
                    avatar_url TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id INTEGER NOT NULL,
                    user_id INTEGER,
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
                )
                """
            )
        ensure_comment_user_id_column(db)
    DB_INIT_DONE = True


def ensure_comment_user_id_column(db):
    if using_postgres():
        db.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES users(id) ON DELETE SET NULL")
        return

    columns = db.execute("PRAGMA table_info(comments)").fetchall()
    if not any(column["name"] == "user_id" for column in columns):
        db.execute("ALTER TABLE comments ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL")


def admin_username():
    return os.environ.get("RAKETEX_ADMIN_USER", "admin")


def admin_password_hash():
    password_hash = os.environ.get("RAKETEX_ADMIN_PASSWORD_HASH")
    if password_hash:
        return password_hash
    return generate_password_hash(os.environ.get("RAKETEX_ADMIN_PASSWORD", "raketex123"))


def is_admin():
    return session.get("is_admin") is True


def is_signed_in():
    return is_admin() or bool(session.get("user_id") or session.get("user_name"))


def current_user_name():
    return session.get("user_name") or ("admin" if is_admin() else "")


def current_user_id():
    return session.get("user_id")


def require_admin():
    if not is_admin():
        flash("Sign in first.", "warn")
        return redirect(url_for("login"))
    return None


def google_sign_in_enabled():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def google_oauth_client():
    from authlib.integrations.flask_client import OAuth

    oauth = OAuth(app)
    return oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def image_is_allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def using_blob_storage():
    return bool(BLOB_READ_WRITE_TOKEN)


def is_remote_image(image_ref):
    if not image_ref:
        return False
    parsed = urlparse(image_ref)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_private_blob_ref(image_ref):
    return bool(image_ref and image_ref.startswith(PRIVATE_BLOB_PREFIX))


def private_blob_pathname(image_ref):
    return image_ref.removeprefix(PRIVATE_BLOB_PREFIX)


def post_image_src(image_ref):
    if not image_ref:
        return ""
    if is_private_blob_ref(image_ref):
        return url_for("blob_image", pathname=private_blob_pathname(image_ref))
    if is_remote_image(image_ref):
        return image_ref
    return url_for("uploaded_file", filename=image_ref)


def blob_result_value(blob, key):
    if isinstance(blob, dict):
        return blob.get(key)
    return getattr(blob, key)


def save_uploaded_image(file_storage):
    if not file_storage or file_storage.filename == "":
        return None
    if not image_is_allowed(file_storage.filename):
        raise ValueError("Use png, jpg, jpeg, gif or webp images.")

    original_name = secure_filename(file_storage.filename)
    suffix = Path(original_name).suffix.lower()
    filename = f"{uuid.uuid4().hex}{suffix}"

    if using_blob_storage():
        from vercel.blob import BlobClient

        pathname = f"uploads/{filename}"
        content = file_storage.read()
        content_type = file_storage.mimetype or None
        client = BlobClient()

        try:
            blob = client.put(
                pathname,
                content,
                access="public",
                content_type=content_type,
                add_random_suffix=False,
            )
            return blob_result_value(blob, "url")
        except Exception as exc:
            if "private store" not in str(exc).lower():
                raise

            blob = client.put(
                pathname,
                content,
                access="private",
                content_type=content_type,
                add_random_suffix=False,
            )
            return f"{PRIVATE_BLOB_PREFIX}{blob_result_value(blob, 'pathname')}"

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_storage.save(UPLOAD_DIR / filename)
    return filename


def search_public_posts(search_query):
    with get_db() as db:
        if search_query:
            like_query = f"%{search_query.lower()}%"
            if using_postgres():
                return db.execute(
                    """
                    SELECT * FROM posts
                    WHERE published = TRUE
                      AND (
                        LOWER(title) LIKE %s
                        OR LOWER(body) LIKE %s
                        OR LOWER(category) LIKE %s
                      )
                    ORDER BY created_at DESC
                    """,
                    (like_query, like_query, like_query),
                ).fetchall()

            return db.execute(
                """
                SELECT * FROM posts
                WHERE published = 1
                  AND (
                    LOWER(title) LIKE ?
                    OR LOWER(body) LIKE ?
                    OR LOWER(category) LIKE ?
                  )
                ORDER BY created_at DESC
                """,
                (like_query, like_query, like_query),
            ).fetchall()

        if using_postgres():
            return db.execute(
                """
                SELECT * FROM posts
                WHERE published = TRUE
                ORDER BY created_at DESC
                """
            ).fetchall()

        return db.execute(
            """
            SELECT * FROM posts
            WHERE published = 1
            ORDER BY created_at DESC
            """
        ).fetchall()


def get_post(post_id, include_drafts=False):
    with get_db() as db:
        if using_postgres():
            return db.execute(
                """
                SELECT * FROM posts
                WHERE id = %s AND (published = TRUE OR %s = TRUE)
                """,
                (post_id, include_drafts),
            ).fetchone()

        return db.execute(
            """
            SELECT * FROM posts
            WHERE id = ? AND (published = 1 OR ? = 1)
            """,
            (post_id, 1 if include_drafts else 0),
        ).fetchone()


def list_comments(post_id):
    with get_db() as db:
        if using_postgres():
            return db.execute(
                """
                SELECT
                    comments.*,
                    COALESCE(users.display_name, comments.author) AS display_author,
                    users.avatar_url AS avatar_url
                FROM comments
                LEFT JOIN users ON users.id = comments.user_id
                WHERE comments.post_id = %s
                ORDER BY comments.created_at ASC, comments.id ASC
                """,
                (post_id,),
            ).fetchall()

        return db.execute(
            """
            SELECT
                comments.*,
                COALESCE(users.display_name, comments.author) AS display_author,
                users.avatar_url AS avatar_url
            FROM comments
            LEFT JOIN users ON users.id = comments.user_id
            WHERE comments.post_id = ?
            ORDER BY comments.created_at ASC, comments.id ASC
            """,
            (post_id,),
        ).fetchall()


def create_comment(post_id, user_id, author, body):
    with get_db() as db:
        if using_postgres():
            db.execute(
                """
                INSERT INTO comments (post_id, user_id, author, body, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (post_id, user_id, author, body, now_iso()),
            )
            return

        db.execute(
            """
            INSERT INTO comments (post_id, user_id, author, body, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, user_id, author, body, now_iso()),
        )


def upsert_google_user(google_id, email, display_name, avatar_url):
    timestamp = now_iso()
    with get_db() as db:
        if using_postgres():
            user = db.execute("SELECT * FROM users WHERE google_id = %s", (google_id,)).fetchone()
            if user:
                db.execute(
                    """
                    UPDATE users
                    SET email = %s, display_name = %s, avatar_url = %s
                    WHERE id = %s
                    """,
                    (email, display_name, avatar_url, user["id"]),
                )
                return db.execute("SELECT * FROM users WHERE id = %s", (user["id"],)).fetchone()

            return db.execute(
                """
                INSERT INTO users (google_id, email, display_name, avatar_url, created_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (google_id, email, display_name, avatar_url, timestamp),
            ).fetchone()

        user = db.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()
        if user:
            db.execute(
                """
                UPDATE users
                SET email = ?, display_name = ?, avatar_url = ?
                WHERE id = ?
                """,
                (email, display_name, avatar_url, user["id"]),
            )
            return db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()

        cursor = db.execute(
            """
            INSERT INTO users (google_id, email, display_name, avatar_url, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (google_id, email, display_name, avatar_url, timestamp),
        )
        return db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()


def get_comment(comment_id):
    with get_db() as db:
        if using_postgres():
            return db.execute("SELECT * FROM comments WHERE id = %s", (comment_id,)).fetchone()
        return db.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()


def delete_comment_by_id(comment_id):
    with get_db() as db:
        if using_postgres():
            db.execute("DELETE FROM comments WHERE id = %s", (comment_id,))
            return
        db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))


def list_admin_posts():
    with get_db() as db:
        return db.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()


def create_post(title, category, body, image_filename, published):
    timestamp = now_iso()
    with get_db() as db:
        if using_postgres():
            db.execute(
                """
                INSERT INTO posts (title, category, body, image_filename, published, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (title, category, body, image_filename, published, timestamp, timestamp),
            )
            return

        db.execute(
            """
            INSERT INTO posts (title, category, body, image_filename, published, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (title, category, body, image_filename, 1 if published else 0, timestamp, timestamp),
        )


def update_post(post_id, title, category, body, image_filename, published):
    with get_db() as db:
        if using_postgres():
            db.execute(
                """
                UPDATE posts
                SET title = %s, category = %s, body = %s, image_filename = %s, published = %s, updated_at = %s
                WHERE id = %s
                """,
                (title, category, body, image_filename, published, now_iso(), post_id),
            )
            return

        db.execute(
            """
            UPDATE posts
            SET title = ?, category = ?, body = ?, image_filename = ?, published = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, category, body, image_filename, 1 if published else 0, now_iso(), post_id),
        )


def delete_post_by_id(post_id):
    with get_db() as db:
        if using_postgres():
            db.execute("DELETE FROM posts WHERE id = %s", (post_id,))
            return
        db.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM posts WHERE id = ?", (post_id,))


def storage_check():
    try:
        init_db()
        with get_db() as db:
            if using_postgres():
                db.execute("SELECT 1").fetchone()
            else:
                db.execute("SELECT 1").fetchone()
        return None
    except Exception as exc:
        app.logger.exception("Storage check failed")
        return str(exc)


@app.before_request
def ensure_database():
    if request.endpoint in {"healthz", "asset_file", "workspace_asset_file"}:
        return None

    issues = storage_config_issues()
    if issues:
        return render_template("setup_error.html", issues=issues), 503

    try:
        init_db()
    except Exception as exc:
        app.logger.exception("Storage initialization failed")
        return render_template("setup_error.html", issues=["Storage initialization failed."], detail=str(exc)), 500
    return None


@app.context_processor
def inject_admin_state():
    return {
        "is_admin": is_admin(),
        "is_signed_in": is_signed_in(),
        "current_user_name": current_user_name(),
        "google_sign_in_enabled": google_sign_in_enabled(),
        "post_image_src": post_image_src,
    }


@app.route("/healthz")
def healthz():
    issues = storage_config_issues()
    storage_error = None if issues else storage_check()
    return jsonify(
        {
            "ok": not issues and storage_error is None,
            "running_on_vercel": running_on_vercel(),
            "using_postgres": using_postgres(),
            "using_blob_storage": using_blob_storage(),
            "database_env": "DATABASE_URL" if os.environ.get("DATABASE_URL") else "POSTGRES_URL" if os.environ.get("POSTGRES_URL") else None,
            "issues": issues,
            "storage_error": storage_error,
        }
    )


@app.route("/")
def home():
    search_query = request.args.get("q", "").strip()
    posts = search_public_posts(search_query)
    return render_template("home.html", posts=posts, search_query=search_query)


@app.route("/post/<int:post_id>")
def post_detail(post_id):
    post = get_post(post_id, include_drafts=is_admin())
    if post is None:
        return render_template("404.html"), 404
    comments = list_comments(post_id)
    return render_template("post.html", post=post, comments=comments)


@app.route("/post/<int:post_id>/comments", methods=["POST"])
def add_comment(post_id):
    post = get_post(post_id, include_drafts=is_admin())
    if post is None:
        return render_template("404.html"), 404
    if not is_signed_in():
        flash("Sign in with user first.", "warn")
        return redirect(url_for("login") + f"?next={url_for('post_detail', post_id=post_id)}#comments")

    author = current_user_name()
    body = request.form.get("body", "").strip()

    if not body:
        flash("Write a comment first.", "warn")
        return redirect(url_for("post_detail", post_id=post_id) + "#comments")
    if len(body) > 1200:
        flash("Comment is too long. Keep it under 1200 characters.", "warn")
        return redirect(url_for("post_detail", post_id=post_id) + "#comments")

    try:
        create_comment(post_id, current_user_id(), author, body)
    except Exception as exc:
        app.logger.exception("Comment creation failed")
        flash(f"Comment failed: {exc}", "warn")
        return redirect(url_for("post_detail", post_id=post_id) + "#comments")

    flash("Comment posted.", "ok")
    return redirect(url_for("post_detail", post_id=post_id) + "#comments")


@app.route("/comments/<int:comment_id>/delete", methods=["POST"])
def delete_comment(comment_id):
    blocked = require_admin()
    if blocked:
        return blocked

    comment = get_comment(comment_id)
    if comment is None:
        flash("Comment not found.", "warn")
        return redirect(url_for("home"))

    post_id = comment["post_id"]
    delete_comment_by_id(comment_id)
    flash("Comment deleted.", "ok")
    return redirect(url_for("post_detail", post_id=post_id) + "#comments")


@app.route("/admin")
def admin():
    blocked = require_admin()
    if blocked:
        return blocked
    posts = list_admin_posts()
    return render_template("admin.html", posts=posts)


@app.route("/admin/login", methods=["GET", "POST"])
@app.route("/user/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == admin_username() and check_password_hash(admin_password_hash(), password):
            session.clear()
            session["is_admin"] = True
            session["user_name"] = admin_username()
            flash("Signed in.", "ok")
            return redirect(request.args.get("next") or url_for("admin"))
        flash("Wrong username or password.", "warn")
    return render_template("login.html")


@app.route("/login/google")
def google_login():
    if not google_sign_in_enabled():
        flash("Google sign-in is not configured yet.", "warn")
        return redirect(url_for("login"))

    google = google_oauth_client()
    redirect_uri = GOOGLE_REDIRECT_URI or url_for("google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def google_callback():
    if not google_sign_in_enabled():
        flash("Google sign-in is not configured yet.", "warn")
        return redirect(url_for("login"))

    google = google_oauth_client()
    token = google.authorize_access_token()
    userinfo = token.get("userinfo") or google.parse_id_token(token)
    google_id = userinfo.get("sub")
    display_name = (userinfo.get("name") or userinfo.get("email") or "user").strip()

    if not google_id:
        flash("Google sign-in did not return a user id.", "warn")
        return redirect(url_for("login"))

    user = upsert_google_user(
        google_id=google_id,
        email=userinfo.get("email"),
        display_name=display_name[:80],
        avatar_url=userinfo.get("picture"),
    )
    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["display_name"]
    session["is_admin"] = False
    flash("Signed in.", "ok")
    return redirect(url_for("home"))


@app.route("/admin/logout", methods=["POST"])
@app.route("/user/logout", methods=["POST"])
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
        except Exception as exc:
            app.logger.exception("Image upload failed")
            flash(f"Image upload failed: {exc}", "warn")
            return render_template("post_form.html", post=None)

        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        category = request.form.get("category", "build log").strip() or "build log"
        published = request.form.get("published") == "on"

        if not title or not body:
            flash("Title and body are required.", "warn")
            return render_template("post_form.html", post=None)

        try:
            create_post(title, category, body, image_filename, published)
        except Exception as exc:
            app.logger.exception("Post creation failed")
            flash(f"Post creation failed: {exc}", "warn")
            return render_template("post_form.html", post=None)
        flash("Post created.", "ok")
        return redirect(url_for("admin"))
    return render_template("post_form.html", post=None)


@app.route("/admin/posts/<int:post_id>/edit", methods=["GET", "POST"])
def edit_post(post_id):
    blocked = require_admin()
    if blocked:
        return blocked
    post = get_post(post_id, include_drafts=True)
    if post is None:
        return render_template("404.html"), 404

    if request.method == "POST":
        try:
            new_image = save_uploaded_image(request.files.get("image"))
        except ValueError as exc:
            flash(str(exc), "warn")
            return render_template("post_form.html", post=post)
        except Exception as exc:
            app.logger.exception("Image upload failed")
            flash(f"Image upload failed: {exc}", "warn")
            return render_template("post_form.html", post=post)

        image_filename = new_image or post["image_filename"]
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        category = request.form.get("category", "build log").strip() or "build log"
        published = request.form.get("published") == "on"

        if not title or not body:
            flash("Title and body are required.", "warn")
            return render_template("post_form.html", post=post)

        try:
            update_post(post_id, title, category, body, image_filename, published)
        except Exception as exc:
            app.logger.exception("Post update failed")
            flash(f"Post update failed: {exc}", "warn")
            return render_template("post_form.html", post=post)
        flash("Post updated.", "ok")
        return redirect(url_for("admin"))

    return render_template("post_form.html", post=post)


@app.route("/admin/posts/<int:post_id>/delete", methods=["POST"])
def delete_post(post_id):
    blocked = require_admin()
    if blocked:
        return blocked
    try:
        delete_post_by_id(post_id)
    except Exception as exc:
        app.logger.exception("Post deletion failed")
        flash(f"Post deletion failed: {exc}", "warn")
        return redirect(url_for("admin"))
    flash("Post deleted.", "ok")
    return redirect(url_for("admin"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/blob/<path:pathname>")
def blob_image(pathname):
    from vercel.blob import BlobClient

    blob = BlobClient().get(pathname, access="private")
    content = blob_result_value(blob, "content")
    content_type = blob_result_value(blob, "content_type") or "application/octet-stream"
    return Response(content, mimetype=content_type)


@app.route("/assets/<path:filename>")
def asset_file(filename):
    return send_from_directory(BASE_DIR / "assets", filename)


@app.route("/workspace-assets/<path:filename>")
def workspace_asset_file(filename):
    return send_from_directory(BASE_DIR.parent, filename)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
