"""
Box upload helpers for the EDGAR scraper.

.env values used (via os.getenv — do not hardcode secrets here):
  BOX_CLIENT_ID
  BOX_CLIENT_SECRET
  BOX_INITIAL_ACCESS_TOKEN
  BOX_REFRESH_TOKEN
  FOLDER                  (upload path under All Files)

Developer token is not required. When Box rotates tokens, store_tokens
writes the new access/refresh pair back into .env.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from boxsdk import Client, OAuth2
from boxsdk.exception import BoxAPIException
import dotenv

# Project root (…/Webscraper), not this file's box/ package dir.
ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"

dotenv.load_dotenv(ENV_PATH)

# Files larger than this use Box chunked upload.
SIMPLE_UPLOAD_MAX_BYTES = 50 * 1024 * 1024

# Cache Box folder ids by path so we do not re-walk from root every upload.
_folder_id_cache: dict[str, str] = {}


def _upload_folder() -> str:
    folder = os.getenv("FOLDER", "EDGAR log files/Unfiltered").strip()
    return folder.strip("/") or "EDGAR log files/Unfiltered"


def _year_from_log_filename(local_csv_path: Path) -> str:
    """Extract the 4-digit year from a logYYYYMMDD filename."""
    match = re.search(r"log(\d{4})", local_csv_path.name, re.IGNORECASE)
    if not match:
        raise ValueError(f"Could not extract year from log filename: {local_csv_path.name}")
    return match.group(1)


def _upload_folder_path(
    local_csv_path: Path,
    destination_folder: str | None = None,
) -> str:
    """Build Box folder path: <base>/<year>, e.g. EDGAR log files/Filtered/2007."""
    base_folder = destination_folder if destination_folder else _upload_folder()
    year = _year_from_log_filename(local_csv_path)
    return f"{base_folder.strip('/')}/{year}"


def update_env_vars(updates: dict[str, str]) -> None:
    """Update keys in .env in place (create file / append missing keys)."""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)

    remaining = dict(updates)
    new_lines: list[str] = []
    for line in lines:
        matched = False
        for key, value in list(remaining.items()):
            if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                new_lines.append(f"{key}={value}\n")
                remaining.pop(key)
                matched = True
                break
        if not matched:
            new_lines.append(line if line.endswith("\n") else line + "\n")

    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    for key, value in remaining.items():
        new_lines.append(f"{key}={value}\n")

    ENV_PATH.write_text("".join(new_lines), encoding="utf-8")


def _store_tokens(access_token: str, refresh_token: str) -> None:
    """Persist rotated tokens (Box refresh tokens are single-use)."""
    update_env_vars(
        {
            "BOX_INITIAL_ACCESS_TOKEN": access_token,
            "BOX_REFRESH_TOKEN": refresh_token,
        }
    )
    os.environ["BOX_INITIAL_ACCESS_TOKEN"] = access_token
    os.environ["BOX_REFRESH_TOKEN"] = refresh_token


def clear_folder_id_cache() -> None:
    """Drop cached folder ids (call after reauth or path changes)."""
    _folder_id_cache.clear()


def get_box_client() -> Client:
    """Build a Box client using OAuth refresh credentials from .env."""
    client_id = os.getenv("BOX_CLIENT_ID", "").strip()
    client_secret = os.getenv("BOX_CLIENT_SECRET", "").strip()
    access_token = os.getenv("BOX_INITIAL_ACCESS_TOKEN", "").strip()
    refresh_token = os.getenv("BOX_REFRESH_TOKEN", "").strip()

    missing = [
        name
        for name, value in (
            ("BOX_CLIENT_ID", client_id),
            ("BOX_CLIENT_SECRET", client_secret),
            ("BOX_INITIAL_ACCESS_TOKEN", access_token),
            ("BOX_REFRESH_TOKEN", refresh_token),
        )
        if not value
    ]
    if missing:
        raise ValueError("Missing Box credentials in .env: " + ", ".join(missing))

    oauth = OAuth2(
        client_id=client_id,
        client_secret=client_secret,
        access_token=access_token,
        refresh_token=refresh_token,
        store_tokens=_store_tokens,
    )
    return Client(oauth)


def reauth_box_client() -> Client:
    """
    Reload tokens from .env, force a refresh, and return a new client.

    Clears the folder-id cache so subsequent calls re-resolve paths with the
    fresh client. store_tokens persists the rotated pair back to .env.
    """
    clear_folder_id_cache()
    dotenv.load_dotenv(ENV_PATH, override=True)
    client = get_box_client()
    # Force refresh now so expired/invalid access tokens are replaced up front.
    client.auth.refresh(None)
    print("    box: re-authenticated (token refresh)")
    return client


def _is_auth_error(error: BaseException) -> bool:
    if isinstance(error, BoxAPIException):
        if error.status == 401:
            return True
        headers = error.headers or {}
        www = str(headers.get("www-authenticate", "")).lower()
        if "invalid_token" in www:
            return True
    return "invalid_token" in str(error).lower()


def _folder_id_for_path(client: Client, path: str) -> str:
    """
    Walk existing folders under root (id '0') and return the leaf folder id.

    Does not create folders. Name matching is case-insensitive so paths like
    'EDGAR log files/...' resolve even if Box casing differs slightly.
    """
    normalized = path.strip("/")
    if normalized in _folder_id_cache:
        return _folder_id_cache[normalized]

    folder_id = "0"
    walked = ""
    for name in normalized.split("/"):
        if not name:
            continue
        walked = f"{walked}/{name}" if walked else name
        if walked in _folder_id_cache:
            folder_id = _folder_id_cache[walked]
            continue

        # Case-insensitive match against existing children only (never create).
        child = next(
            (
                item
                for item in client.folder(folder_id).get_items()
                if item.type == "folder" and item.name.lower() == name.lower()
            ),
            None,
        )
        if child is None:
            raise FileNotFoundError(
                f"Box folder not found (no create): '{walked}' under path '{normalized}'"
            )
        folder_id = child.id
        _folder_id_cache[walked] = folder_id

    return folder_id


def _existing_file(client: Client, folder_id: str, file_name: str):
    """Return a file with this name in the folder, or None."""
    return next(
        (
            item
            for item in client.folder(folder_id).get_items()
            if item.type == "file" and item.name == file_name
        ),
        None,
    )


def verify_uploaded_file(
    client: Client,
    local_csv_path: str | Path,
    destination_folder: str | None = None,
) -> tuple[bool, Client]:
    """
    Return (found, client). client may be a re-authed instance after a 401.
    """
    local_csv_path = Path(local_csv_path)

    def _check(active: Client) -> bool:
        upload_folder = _upload_folder_path(
            local_csv_path, destination_folder=destination_folder
        )
        folder_id = _folder_id_for_path(active, upload_folder)
        return _existing_file(active, folder_id, local_csv_path.name) is not None

    try:
        return _check(client), client
    except BoxAPIException as error:
        if not _is_auth_error(error):
            raise
        print("    box: 401 during verify — re-authenticating and retrying")
        client = reauth_box_client()
        return _check(client), client


def upload_csv(
    local_csv_path: str | Path,
    client: Client | None = None,
    destination_folder: str | None = None,
) -> tuple[str, Client]:
    """Upload one CSV. Returns (destination_path, client)."""
    local_csv_path = Path(local_csv_path)
    if not local_csv_path.exists():
        raise FileNotFoundError(f"Local CSV not found: {local_csv_path}")
    if local_csv_path.suffix.lower() != ".csv":
        raise ValueError(f"Expected a .csv file, got: {local_csv_path}")

    if client is None:
        client = get_box_client()

    upload_folder = _upload_folder_path(local_csv_path, destination_folder=destination_folder)
    folder_id = _folder_id_for_path(client, upload_folder)
    file_name = local_csv_path.name
    file_size = local_csv_path.stat().st_size
    existing = _existing_file(client, folder_id, file_name)

    if existing is not None:
        if file_size <= SIMPLE_UPLOAD_MAX_BYTES:
            existing.update_contents(str(local_csv_path))
        else:
            existing.get_chunked_uploader(str(local_csv_path)).start()
    elif file_size <= SIMPLE_UPLOAD_MAX_BYTES:
        client.folder(folder_id).upload(str(local_csv_path), file_name=file_name)
    else:
        client.folder(folder_id).get_chunked_uploader(
            str(local_csv_path),
            file_name=file_name,
        ).start()

    return f"{upload_folder}/{file_name}", client


def upload_csv_safe(
    local_csv_path: str | Path,
    client: Client | None = None,
    destination_folder: str | None = None,
) -> tuple[str | None, str | None, Client]:
    """
    Upload one CSV. Returns (destination, reason, client).

    On 401 invalid_token, reloads credentials, refreshes, and retries once.
    The returned client may be new — callers should keep using it.
    """
    # print("upload_csv_safe called - returning None for testing")
    # return

    if client is None:
        client = get_box_client()

    try:
        print("    uploading CSV to Box")
        destination, client = upload_csv(
            local_csv_path, client=client, destination_folder=destination_folder
        )
        print(f"    uploaded -> {destination}")
        return destination, None, client
    except BoxAPIException as error:
        print(f"    box: {type(error).__name__}: {error}")
        if _is_auth_error(error):
            print("    box: 401 during upload — re-authenticating and retrying")
            try:
                client = reauth_box_client()
                destination, client = upload_csv(
                    local_csv_path,
                    client=client,
                    destination_folder=destination_folder,
                )
                print(f"    uploaded -> {destination}")
                return destination, None, client
            except (BoxAPIException, ValueError, FileNotFoundError, OSError) as retry_error:
                reason = f"box: {type(retry_error).__name__}: {retry_error}"
                print(f"    upload failed after reauth: {reason}")
                return None, reason, client
        reason = f"box: {type(error).__name__}: {error}"
        print(f"    upload failed: {reason}")
        return None, reason, client
    except (ValueError, FileNotFoundError, OSError) as error:
        reason = f"box: {type(error).__name__}: {error}"
        print(f"    upload failed: {reason}")
        return None, reason, client


def download_csv(
    file_name: str,
    client: Client | None = None,
    destination_folder: str | None = None,
    local_path: str | Path | None = None,
) -> tuple[Path, Client]:
    """
    Download one CSV from Box by name under <destination_folder>/<year>/.
    Year is parsed from logYYYYMMDD in file_name.
    """
    if client is None:
        client = get_box_client()

    if local_path is None:
        local_path = Path(file_name)
    else:
        local_path = Path(local_path)

    # Build the same year-scoped folder path used by uploads.
    upload_folder = _upload_folder_path(
        Path(file_name), destination_folder=destination_folder
    )
    folder_id = _folder_id_for_path(client, upload_folder)
    existing = _existing_file(client, folder_id, file_name)
    if existing is None:
        raise FileNotFoundError(
            f"Box file not found: {upload_folder}/{file_name}"
        )

    # Write the full remote file to local_path.
    with open(local_path, "wb") as out_file:
        existing.download_to(out_file)

    return local_path, client


def download_csv_safe(
    file_name: str,
    client: Client | None = None,
    destination_folder: str | None = None,
    local_path: str | Path | None = None,
) -> tuple[Path | None, str | None, Client]:
    """
    Download one CSV from Box. Returns (local_path, reason, client).

    On 401 invalid_token, reauth and retry once.
    """
    if client is None:
        client = get_box_client()

    try:
        path, client = download_csv(
            file_name,
            client=client,
            destination_folder=destination_folder,
            local_path=local_path,
        )
        print(f"    downloaded <- {_upload_folder_path(Path(file_name), destination_folder)}/{file_name}")
        return path, None, client
    except BoxAPIException as error:
        if _is_auth_error(error):
            print("    box: 401 during download — re-authenticating and retrying")
            try:
                client = reauth_box_client()
                path, client = download_csv(
                    file_name,
                    client=client,
                    destination_folder=destination_folder,
                    local_path=local_path,
                )
                print(
                    f"    downloaded <- "
                    f"{_upload_folder_path(Path(file_name), destination_folder)}/{file_name}"
                )
                return path, None, client
            except (BoxAPIException, ValueError, FileNotFoundError, OSError) as retry_error:
                reason = f"box: {type(retry_error).__name__}: {retry_error}"
                print(f"    download failed after reauth: {reason}")
                return None, reason, client
        reason = f"box: {type(error).__name__}: {error}"
        print(f"    download failed: {reason}")
        return None, reason, client
    except (ValueError, FileNotFoundError, OSError) as error:
        reason = f"box: {type(error).__name__}: {error}"
        print(f"    download failed: {reason}")
        return None, reason, client


if __name__ == "__main__":
    print("box_upload.py executed")
