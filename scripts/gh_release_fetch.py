#!/usr/bin/env python3
# /// script
# dependencies = [
#   "httpx",
# ]
# ///
"""Download GitHub release assets for specified tags with hash verification.

Usage examples:
  python gh_release_fetch.py --repo pypa/pip --tags 24.0 23.3 --download-dir downloads
  python gh_release_fetch.py --repo https://github.com/psf/requests --tags latest
  python gh_release_fetch.py --repo pypa/pip --tags latest 24.0 --force-latest

Semantics:
  - If --tags provided: download assets for specified tags. Support 'latest' tag for latest release.
  - If 'latest' tag included: always check for updates and re-download if latest release changed.
  - Hash verification: streaming SHA256 while downloading then re-hash file for integrity check.
  - A manifest (release_manifest.json) aggregates metadata & hashes; updated idempotently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List

import httpx

# ----------------------------- Constants & Defaults ----------------------------
GITHUB_API_BASE = "https://api.github.com"
DEFAULT_PER_PAGE = 100
HASH_ALGO = "sha256"
MANIFEST_FILENAME = "release_manifest.json"
REQUEST_TIMEOUT = 30.0  # seconds
LATEST_TAG_ALIAS = "latest"

LOG_LEVEL = os.getenv("GH_RELEASE_FETCH_LOG", "INFO").upper()


# --------------------------------- Data Models ---------------------------------
@dataclass(slots=True)
class AssetRecord:
    repo: str
    release_tag: str
    release_id: int
    asset_id: int
    asset_name: str
    size: int
    download_url: str
    hash_algo: str
    hash_value: str
    path: str


# --------------------------------- Logging Setup -------------------------------
def setup_logging() -> None:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# --------------------------------- Utilities -----------------------------------
def parse_repo(repo: str) -> str:
    """Normalize repo spec to 'owner/name'. Accept full URL or plain owner/name."""
    if repo.startswith("http://") or repo.startswith("https://"):
        m = re.match(r"https?://github\.com/([^/]+/[^/]+)(?:/|$)", repo)
        if not m:
            raise ValueError(f"Unsupported GitHub URL: {repo}")
        return m.group(1)
    if repo.count("/") != 1:
        raise ValueError("Repo must be in 'owner/name' format")
    return repo


def build_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        logging.info(f"Using GitHub token: {token[:4]}...{token[-4:]}")
        headers["Authorization"] = f"Bearer {token}"
    return headers


def build_proxy_config() -> str | None:
    """Build proxy configuration from environment variables."""
    # httpx automatically respects HTTP_PROXY and HTTPS_PROXY environment variables
    # But we can also explicitly set a proxy if needed
    http_proxy = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    https_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")

    # For GitHub API, we typically use HTTPS, so prioritize HTTPS proxy
    proxy = https_proxy or http_proxy
    return proxy


def load_existing_manifest(path: Path) -> List[dict]:
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception as exc:  # noqa: BLE001
        logging.warning("Failed reading manifest %s: %s", path, exc)
    return []


def save_manifest(path: Path, rows: Iterable[AssetRecord]) -> None:
    existing = load_existing_manifest(path)
    existing_map = {(r.get("asset_id"), r.get("hash_value")): r for r in existing}
    new_items = []
    for rec in rows:
        key = (rec.asset_id, rec.hash_value)
        if key not in existing_map:
            new_items.append(asdict(rec))
    if not new_items:
        logging.info("Manifest unchanged (%s)", path)
        return
    merged = existing + new_items
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    logging.info("Updated manifest with %d new assets (%s)", len(new_items), path)


# --------------------------------- Fetch Logic ---------------------------------


def get_latest_release(client: httpx.Client, repo: str) -> dict | None:
    """Get the latest release from GitHub API."""
    url = f"{GITHUB_API_BASE}/repos/{repo}/releases/latest"
    try:
        r = client.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 404:
            logging.warning("No releases found for repository %s", repo)
            return None
        if r.status_code == 403:
            raise RuntimeError(f"GitHub API rate limited or forbidden: {r.text[:200]}")
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        logging.error("Failed to get latest release: %s", exc)
        return None


def get_release_by_tag(client: httpx.Client, repo: str, tag: str) -> dict | None:
    """Get a specific release by tag name."""
    if tag == LATEST_TAG_ALIAS:
        return get_latest_release(client, repo)

    url = f"{GITHUB_API_BASE}/repos/{repo}/releases/tags/{tag}"
    try:
        r = client.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 404:
            logging.warning(
                "Release with tag '%s' not found for repository %s", tag, repo
            )
            return None
        if r.status_code == 403:
            raise RuntimeError(f"GitHub API rate limited or forbidden: {r.text[:200]}")
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        logging.error("Failed to get release for tag '%s': %s", tag, exc)
        return None


def has_latest_changed(latest_manifest_path: Path, current_latest_tag: str) -> bool:
    """Check if the latest release has changed since last download."""
    if not latest_manifest_path.is_file():
        return True

    try:
        with latest_manifest_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        stored_latest = data.get("latest_tag")
        return stored_latest != current_latest_tag
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "Failed reading latest manifest %s: %s", latest_manifest_path, exc
        )
        return True


def save_latest_tag(latest_manifest_path: Path, latest_tag: str) -> None:
    """Save the current latest tag to manifest."""
    latest_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with latest_manifest_path.open("w", encoding="utf-8") as f:
        json.dump({"latest_tag": latest_tag, "updated_at": None}, f, indent=2)


def should_skip_asset(dest: Path, size: int, force: bool) -> bool:
    if force:
        return False
    if dest.is_file() and dest.stat().st_size == size:
        logging.info("Skip existing asset (size match) %s", dest)
        return True
    return False


def download_asset(client: httpx.Client, url: str, dest: Path) -> str:
    logging.info("Downloading %s -> %s", url, dest)
    hasher = hashlib.new(HASH_ALGO)
    with client.stream(
        "GET", url, follow_redirects=True, timeout=REQUEST_TIMEOUT
    ) as resp:
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes():
                if not chunk:
                    continue
                f.write(chunk)
                hasher.update(chunk)
    stream_hash = hasher.hexdigest()
    # Re-hash for verification.
    verify_hasher = hashlib.new(HASH_ALGO)
    with dest.open("rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            verify_hasher.update(block)
    verify_hash = verify_hasher.hexdigest()
    if stream_hash != verify_hash:
        raise IOError(
            f"Hash mismatch for {dest} (stream {stream_hash} != verify {verify_hash})"
        )
    logging.info("Verified %s %s=%s", dest.name, HASH_ALGO, stream_hash)
    # Write sidecar hash file
    hash_file = dest.with_suffix(dest.suffix + f".{HASH_ALGO}")
    with hash_file.open("w", encoding="utf-8") as f:
        f.write(stream_hash + "\n")
    return stream_hash


def process_release_assets(
    client: httpx.Client,
    repo: str,
    release: dict,
    download_dir: Path,
    force_download: bool = False,
) -> List[AssetRecord]:
    """Process assets for a single release."""
    records: List[AssetRecord] = []
    tag = release.get("tag_name")
    rel_id = release.get("id")

    logging.info("Processing release tag=%s id=%s", tag, rel_id)

    rel_dir = download_dir / repo.split("/")[0] / repo.split("/")[1] / tag
    assets = release.get("assets") or []

    if not assets:
        logging.info("Release %s has no assets", tag)
        return records

    for asset in assets:
        asset_name = asset.get("name")
        size = asset.get("size", 0)
        asset_id = asset.get("id")
        download_url = asset.get("browser_download_url")

        if not download_url:
            logging.warning("Skip asset without download URL: %s", asset_name)
            continue

        dest_path = rel_dir / asset_name

        if should_skip_asset(dest_path, size, force_download):
            # Compute hash for existing file if sidecar missing.
            hash_file = dest_path.with_suffix(dest_path.suffix + f".{HASH_ALGO}")
            if hash_file.is_file():
                with hash_file.open("r", encoding="utf-8") as f:
                    hash_val = f.read().strip()
            else:
                hash_val = compute_hash(dest_path)
            records.append(
                AssetRecord(
                    repo=repo,
                    release_tag=tag,
                    release_id=rel_id,
                    asset_id=asset_id,
                    asset_name=asset_name,
                    size=size,
                    download_url=download_url,
                    hash_algo=HASH_ALGO,
                    hash_value=hash_val,
                    path=str(dest_path),
                )
            )
            continue

        try:
            hash_value = download_asset(client, download_url, dest_path)
        except Exception as exc:  # noqa: BLE001
            logging.error("Failed downloading %s: %s", asset_name, exc)
            continue

        records.append(
            AssetRecord(
                repo=repo,
                release_tag=tag,
                release_id=rel_id,
                asset_id=asset_id,
                asset_name=asset_name,
                size=size,
                download_url=download_url,
                hash_algo=HASH_ALGO,
                hash_value=hash_value,
                path=str(dest_path),
            )
        )

    return records


def process_specified_tags(
    client: httpx.Client,
    repo: str,
    tags: List[str],
    force_latest: bool,
    download_dir: Path,
) -> List[AssetRecord]:
    """Process releases for specified tags."""
    records: List[AssetRecord] = []
    latest_manifest_path = download_dir / f".{repo.replace('/', '_')}_latest.json"

    for tag in tags:
        if tag == LATEST_TAG_ALIAS:
            release = get_latest_release(client, repo)
            if not release:
                logging.warning("Could not get latest release for %s", repo)
                continue

            actual_tag = release.get("tag_name")
            logging.info("Latest release tag: %s", actual_tag)

            # Check if latest has changed
            latest_changed = has_latest_changed(latest_manifest_path, actual_tag)
            force_download = force_latest or latest_changed

            if latest_changed:
                logging.info("Latest release has changed, will download")
                save_latest_tag(latest_manifest_path, actual_tag)
            elif force_latest:
                logging.info("Force latest flag enabled, will re-download")
            else:
                logging.info("Latest release unchanged, checking existing files")

        else:
            release = get_release_by_tag(client, repo, tag)
            if not release:
                logging.warning("Could not get release for tag %s", tag)
                continue
            force_download = False

        release_records = process_release_assets(
            client, repo, release, download_dir, force_download
        )
        records.extend(release_records)

    return records


def compute_hash(path: Path) -> str:
    hasher = hashlib.new(HASH_ALGO)
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            hasher.update(block)
    return hasher.hexdigest()


# --------------------------------- CLI & Main ----------------------------------
def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download GitHub release assets for specified tags with hash verification."
    )
    p.add_argument(
        "--repo", required=True, help="GitHub repository (owner/name or full URL)"
    )
    p.add_argument(
        "--tags",
        nargs="+",
        required=True,
        help="Release tags to download. Use 'latest' for the latest release. Multiple tags supported.",
    )
    p.add_argument(
        "--force-latest",
        action="store_true",
        help="Force re-download assets for latest release even if files exist and latest hasn't changed.",
    )
    p.add_argument(
        "--download-dir",
        default=".",
        help="Directory to store downloaded assets (default: current directory)",
    )
    return p.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    setup_logging()
    try:
        repo = parse_repo(args.repo)
    except ValueError as e:  # noqa: PERF203
        logging.error(str(e))
        return 2

    download_dir = Path(args.download_dir).expanduser().resolve()
    manifest_path = download_dir / MANIFEST_FILENAME

    logging.info(
        "Repo=%s tags=%s force_latest=%s dest=%s",
        repo,
        args.tags,
        args.force_latest,
        download_dir,
    )

    headers = build_headers()
    proxy = build_proxy_config()
    transport = httpx.HTTPTransport(retries=3)

    client_kwargs = {"headers": headers, "transport": transport}
    if proxy:
        logging.info("Using proxy: %s", proxy)
        client_kwargs["proxy"] = proxy

    with httpx.Client(**client_kwargs) as client:
        try:
            records = process_specified_tags(
                client=client,
                repo=repo,
                tags=args.tags,
                force_latest=bool(args.force_latest),
                download_dir=download_dir,
            )
        except Exception as exc:  # noqa: BLE001
            logging.error("Processing failed: %s", exc)
            return 1

    if not records:
        logging.info("No assets processed.")
        return 0
    save_manifest(manifest_path, records)
    logging.info("Done (%d assets).", len(records))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
