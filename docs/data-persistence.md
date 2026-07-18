# Data persistence — don't lose your keys and trade history

Everything QT remembers lives in one folder inside the container: **`/data`**.
It holds:

- `qt.db` — configuration, strategies, the trade journal, and your **encrypted
  Alpaca API keys**.
- `instance.key` — the encryption key those API keys are sealed with. Without
  it, the saved keys are unreadable.
- `backups/` — periodic copies of `qt.db` (see [Backups](#backups)).

For this to survive container updates, `/data` **must be a real bind mount to a
folder on your server**, not a throwaway location inside the container.

## The incident this guards against

On unraid the "Data" path field was filled in the wrong direction, so the
container path (`/data`) was pointed at the host and the host path ended up
inside the container. Docker then satisfied the app's `/data` with an
**anonymous volume**. Everything looked fine — until an image refresh recreated
the container, orphaned that anonymous volume, and QT booted with an empty
database, cheerfully presenting the first-run setup wizard as if that were
normal. Config, keys and history: gone.

Two changes prevent a silent repeat:

1. **The Dockerfile no longer declares `VOLUME /data`.** That line is what
   auto-created the masking anonymous volume. Without it, a missing or
   misdirected mount just writes to the container's ephemeral layer.
2. **A startup detector** compares the device backing `/data` against the root
   filesystem (and consults `/proc/self/mountinfo`). If `/data` is on the
   ephemeral layer, QT logs an error, fires a Slack alert (if configured), and
   shows a red banner in the UI. It is intentionally conservative: it only warns
   when it is confident, so local development never triggers a false alarm.

## Getting the mapping right

The Docker `-v` flag and the unraid dialog both read **`host : container`**:

```
-v /mnt/user/appdata/qt-autotrader : /data
   └────── host path (yours) ──────┘   └ container path (always /data)
```

- **Host path** (left / unraid "Host Path"): a real folder on your server, e.g.
  `/mnt/user/appdata/qt-autotrader`. This is what you back up.
- **Container path** (right / unraid "Container Path"): always exactly `/data`.

If the banner appears, open your container settings and confirm the Data mapping
matches the above, then recreate the container.

## The "keys can't be decrypted" banner

If you ever see *"Saved API keys can't be decrypted"*, the database has
encrypted secrets but `instance.key` is missing — usually because `/data` was
partially restored, or a fresh key was generated over a restored DB. Options:

- **Restore** the original `instance.key` into `/data` (from a backup), or
- **Re-enter** your Alpaca keys in Setup — this writes a new key and re-encrypts.
  Your Alpaca keys are unaffected on Alpaca's side; you're just re-saving them.

## Backups

QT writes periodic SQLite backups of `qt.db` to `/data/backups/` (the last 7 by
default) using SQLite's online backup, which is safe on a live WAL database. The
bar cache (`bars.db`, when it exists) is **not** backed up — it's bulk,
disposable reference data.

### Restoring from a backup

1. Stop the container.
2. In the host's data folder, replace `qt.db` with the chosen
   `backups/qt-YYYYMMDD-HHMMSS.db` (rename it to `qt.db`). Delete any
   `qt.db-wal` / `qt.db-shm` sidecar files.
3. Make sure `instance.key` in that folder is the one that was in use when the
   backup was taken (they belong together — the DB's secrets are encrypted with
   that key).
4. Start the container.
