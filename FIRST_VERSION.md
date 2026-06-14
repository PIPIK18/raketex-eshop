# RAKETEX First Version

This version is a real local backend for posts/build logs.

## What is included

- Public posts page
- Single post pages
- Admin login
- Create/edit/delete posts
- Image uploads
- Draft/published posts
- SQLite database stored in `instance/raketex.db`
- Uploaded images stored in `uploads/`

## Run locally

```powershell
cd "D:\Users\Stepan\Desktop\PIPIK\pythonCodesVSC\raketex-eshop"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

If creating `.venv` gets stuck or fails during `ensurepip`, use this simpler fallback:

```powershell
cd "D:\Users\Stepan\Desktop\PIPIK\pythonCodesVSC\raketex-eshop"
python -m pip install --user -r requirements.txt
python app.py
```

If `.venv` was only partly created and you want to retry it later:

```powershell
Remove-Item -Recurse -Force .venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

Admin:

```text
http://127.0.0.1:5000/admin/login
```

Default local login:

```text
username: admin
password: raketex123
```

## Before putting it online

Set these environment variables on the server:

```text
RAKETEX_SECRET_KEY=long-random-secret
RAKETEX_ADMIN_USER=your-admin-name
RAKETEX_ADMIN_PASSWORD=your-strong-password
```

For a stronger setup, set `RAKETEX_ADMIN_PASSWORD_HASH` instead of `RAKETEX_ADMIN_PASSWORD`.

## What you still need to do outside code

1. Choose hosting for a Python/Flask app.
2. Buy or connect a domain.
3. Add the environment variables above on the host.
4. Deploy the project files.
5. Make sure HTTPS is enabled.
6. Back up `instance/raketex.db` and `uploads/` regularly.

## Simple deployment options

- Render: easiest beginner-friendly option for Flask.
- Railway: simple app hosting with environment variables.
- VPS: more control, more setup work.

For the first public version, Render or Railway is usually the easiest path.
