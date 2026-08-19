"""
sync_instagram.py — pull new photos from an Instagram profile into a
static-site gallery, update the WORK array in index.html, and push to GitHub.

ONE-TIME SETUP (run these once in a terminal, not from this script):
    pip install -r requirements.txt
    instaloader --login=xovriet
        -> log in interactively (handles 2FA if needed). This creates a
           saved session file so sync_instagram.py never needs your
           password.

NORMAL USE:
    python sync_instagram.py
    (this is what run_silent.vbs / run.bat call for you)

Everything project-specific (paths, the Instagram username, git behaviour)
lives in config.json next to this file — edit that, not this script.
"""

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import instaloader
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r requirements.txt")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "sync_log.txt"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
WORK_ITEM_RE = re.compile(r'\{\s*file:\s*"([^"]+)"\s*,\s*n:\s*(\d+)\s*\}')


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and edit it.")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key in ("repo_path", "images_dir", "index_html", "instagram_username", "target_profile"):
        if not cfg.get(key):
            sys.exit(f"config.json is missing required field: {key}")
    return cfg


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def build_hash_index(images_dir: Path, cache: dict) -> dict:
    """Return {sha256: filename} for every existing image, using/refreshing a cache
    so we don't rehash the whole folder on every run."""
    existing = {p.name for p in images_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS}
    # drop cache entries for files that no longer exist
    cache = {k: v for k, v in cache.items() if v in existing}
    cached_names = set(cache.values())
    for name in existing - cached_names:
        cache[sha256_of(images_dir / name)] = name
    return cache


def get_loader(username: str) -> "instaloader.Instaloader":
    L = instaloader.Instaloader(
        dirname_pattern=str(SCRIPT_DIR / "_tmp_download"),  # overridden per-call below
        filename_pattern="{profile}_{shortcode}",
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        compress_json=False,
        quiet=True,
    )
    try:
        L.load_session_from_file(username)
    except FileNotFoundError:
        sys.exit(
            f"No saved Instaloader session for '{username}'.\n"
            f"Run this once in a terminal first:\n    instaloader --login={username}"
        )
    return L


def parse_work_array(html: str):
    """Return (items, insert_index, max_n) for the WORK array in index.html."""
    m = re.search(r"var\s+WORK\s*=\s*\[", html)
    if not m:
        sys.exit("Could not find 'var WORK = [' in index.html — check config.json's index_html path.")
    close_idx = html.index("];", m.end())
    body = html[m.end():close_idx]
    items = WORK_ITEM_RE.findall(body)
    max_n = max((int(n) for _, n in items), key=int) if items else 0
    existing_files = {f for f, _ in items}
    return existing_files, max_n, close_idx


def append_to_work_array(html: str, close_idx: int, new_files: list, start_n: int, indent="    ") -> str:
    lines = "".join(
        f'{indent}{{ file: "{fname}", n: {start_n + i} }},\n'
        for i, fname in enumerate(new_files)
    )
    return html[:close_idx] + lines + html[close_idx:]


def run_git(repo_path: Path, args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo_path), capture_output=True, text=True
    )


def main():
    cfg = load_config()
    repo_path = Path(cfg["repo_path"])
    images_dir = repo_path / cfg["images_dir"]
    index_html_path = repo_path / cfg["index_html"]
    target_profile = cfg["target_profile"]
    ig_username = cfg["instagram_username"]
    commit_msg_template = cfg.get("commit_message", "Add {count} new photo(s) from Instagram")

    if not images_dir.is_dir():
        sys.exit(f"images_dir does not exist: {images_dir}")
    if not index_html_path.is_file():
        sys.exit(f"index_html does not exist: {index_html_path}")

    log("=== sync run start ===")

    hash_cache_path = SCRIPT_DIR / "sync_hash_cache.json"
    state_path = SCRIPT_DIR / "sync_state.json"
    hash_cache = load_json(hash_cache_path, {})
    state = load_json(state_path, {"synced_shortcodes": []})
    synced = set(state["synced_shortcodes"])

    known_hashes = build_hash_index(images_dir, hash_cache)  # sha256 -> filename
    save_json(hash_cache_path, known_hashes)

    L = get_loader(ig_username)
    try:
        profile = instaloader.Profile.from_username(L.context, target_profile)
    except Exception as e:
        log(f"ERROR fetching profile '{target_profile}': {e}")
        sys.exit(1)

    downloaded_this_run = []  # list of (shortcode, [filenames]) in the order encountered (newest-first)
    tmp_dir = images_dir  # download straight into the live images folder
    L.context.dirname_pattern = str(tmp_dir)

    try:
        for post in profile.get_posts():
            if post.shortcode in synced:
                log(f"Reached already-synced post {post.shortcode}; stopping (incremental run).")
                break
            if post.is_video and not post.typename == "GraphSidecar":
                synced.add(post.shortcode)
                continue

            before = {p.name for p in images_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS}
            try:
                L.download_post(post, target=str(tmp_dir))
            except Exception as e:
                log(f"WARNING: failed to download {post.shortcode}: {e}")
                continue
            after = {p.name for p in images_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS}
            new_files = sorted(after - before)

            kept = []
            for fname in new_files:
                fpath = images_dir / fname
                digest = sha256_of(fpath)
                if digest in known_hashes:
                    log(f"Duplicate of existing '{known_hashes[digest]}' -> removing {fname}")
                    fpath.unlink()
                else:
                    known_hashes[digest] = fname
                    kept.append(fname)

            synced.add(post.shortcode)
            if kept:
                downloaded_this_run.append((post.shortcode, kept))
                log(f"Kept {len(kept)} new file(s) from {post.shortcode}: {kept}")
    finally:
        save_json(hash_cache_path, known_hashes)
        state["synced_shortcodes"] = sorted(synced)
        save_json(state_path, state)

    all_new_files = []
    for _, files in reversed(downloaded_this_run):  # oldest-of-this-batch first
        all_new_files.extend(files)

    if not all_new_files:
        log("No new photos. Nothing to commit.")
        log("=== sync run end ===")
        return

    html = index_html_path.read_text(encoding="utf-8")
    existing_files, max_n, close_idx = parse_work_array(html)
    all_new_files = [f for f in all_new_files if f not in existing_files]
    if not all_new_files:
        log("New files were downloaded but already listed in WORK array. Nothing to commit.")
        log("=== sync run end ===")
        return

    updated_html = append_to_work_array(html, close_idx, all_new_files, max_n + 1)
    index_html_path.write_text(updated_html, encoding="utf-8")
    log(f"Added {len(all_new_files)} entr(y/ies) to WORK array in {index_html_path.name}.")

    status = run_git(repo_path, ["status", "--porcelain"])
    if not status.stdout.strip():
        log("git status is clean; skipping commit/push.")
        log("=== sync run end ===")
        return

    run_git(repo_path, ["add", "-A"])
    commit_msg = commit_msg_template.format(count=len(all_new_files))
    commit = run_git(repo_path, ["commit", "-m", commit_msg])
    log(commit.stdout.strip() or commit.stderr.strip())

    push = run_git(repo_path, ["push"])
    if push.returncode != 0:
        log(f"ERROR: git push failed:\n{push.stderr}")
    else:
        log("Pushed to GitHub.")

    log("=== sync run end ===")


if __name__ == "__main__":
    main()
