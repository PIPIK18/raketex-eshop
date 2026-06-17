# RAKETEX

RAKETEX is a Flask app. It is set up to run locally with SQLite/local uploads and on Vercel with Postgres/Vercel Blob for persistent shared posts and images.

## Local run

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

Admin login defaults:

```text
username: admin
password: raketex123
```

## Local persistence

Posts are stored in SQLite:

```text
instance/raketex.db
```

Uploaded images are stored in:

```text
instance/uploads/
```

## Vercel deployment

1. Import this GitHub repo into Vercel.
2. In Vercel Storage, create/connect a Marketplace Postgres database, preferably Neon.
3. Create/connect a Vercel Blob store with public access for uploaded images.
4. Confirm these environment variables exist in the Vercel project:

```text
DATABASE_URL or POSTGRES_URL
BLOB_READ_WRITE_TOKEN
RAKETEX_SECRET_KEY
RAKETEX_ADMIN_USER
RAKETEX_ADMIN_PASSWORD
```

`RAKETEX_SECRET_KEY` should be a long random value. `RAKETEX_ADMIN_PASSWORD` replaces the local default password.

If your Blob store is private, the app will upload images privately and serve them through Flask at `/blob/...`. Public Blob stores use direct public Blob URLs.

Vercel recognizes `app.py` as a Flask entrypoint, so no static `index.html` is needed.

## Optional local production-like run

To test with hosted storage locally, pull Vercel environment variables and run the app:

```text
vercel env pull
python app.py
```

Without Postgres/Blob env vars, the app automatically falls back to SQLite/local uploads.
