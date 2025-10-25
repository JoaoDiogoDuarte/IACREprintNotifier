#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import datetime
import hashlib
import logging
import os
import time
import json
from pathlib import Path

import feedparser
import requests
from fuzzywuzzy import fuzz
from pyzotero import zotero

# ----------------------------------------------------------------------
# ── CONFIGURATION (env vars take precedence over the hard‑coded values)
# ----------------------------------------------------------------------
FEED_URL = "https://eprint.iacr.org/rss/rss.xml"
SCRIPT_DIR = Path(__file__).resolve().parent

TMP_PDF_DIR = SCRIPT_DIR / "tmp_pdfs"          # ← folder beside the script
TMP_PDF_DIR.mkdir(parents=True, exist_ok=True)  # create it once at import time

# ---- Zotero credentials -------------------------------------------------
ZOTERO_USER_ID_HARD = ""   # e.g. "1234567"
ZOTERO_API_KEY_HARD = ""   # e.g. "abcd1234..."

def get_zotero_credentials():
    uid = os.getenv("ZOTERO_USER_ID") or ZOTERO_USER_ID_HARD
    key = os.getenv("ZOTERO_API_KEY") or ZOTERO_API_KEY_HARD
    if not uid or not key:
        raise RuntimeError("Zotero credentials missing – set env vars or hard‑code them.")
    return uid, key

CACHE_PATH = SCRIPT_DIR / ".zotero_index.json"

def load_cached_index():
    if CACHE_PATH.is_file():
        try:
            with CACHE_PATH.open() as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cached_index(index):
    with CACHE_PATH.open("w") as f:
        json.dump(index, f)

def get_existing_items(zot, collection_key):
    cached = load_cached_index()
    if cached.get("collection_key") == collection_key:
        return cached["index"]

    # … (the pagination loop from earlier) …
    index = {...}  # built as shown above
    save_cached_index({"collection_key": collection_key, "index": index})
    return index

# ---- Email -----------------------------------------------------
EMAIL_RECIPIENT_HARD = ""   # e.g. "you@example.com"
EMAIL_DOMAIN_HARD = ""   # e.g. "sandbox123..."
EMAIL_API_KEY_HARD = ""   # e.g. "932876..."

def get_recipient():
    return os.getenv("MG_TO") or EMAIL_RECIPIENT_HARD

def get_mailgun_api_key():
    return os.getenv("MG_API_KEY") or EMAIL_API_KEY_HARD

def get_mailgun_domain():
    return os.getenv("MG_DOMAIN") or EMAIL_DOMAIN_HARD

# ---- Zotero collection name -----------------------------------------------
ZOTERO_COLLECTION_NAME = "Email Updates"

# ----------------------------------------------------------------------
# ── COMMAND‑LINE & LOGGING --------------------------------------------
# ----------------------------------------------------------------------
def parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Watch the IACR RSS feed for papers by a set of authors."
    )
    p.add_argument("-d", "--debug", action="store_true", help="Enable debug output")
    return p.parse_args()

def configure_logger(debug: bool) -> logging.Logger:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        level=level,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)

log = None   # will be set in __main__

# ----------------------------------------------------------------------
# ── SMALL HELPERS -------------------------------------------------------
# ----------------------------------------------------------------------

def title_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8")).hexdigest()

def read_existing_hashes() -> set:
    path = SCRIPT_DIR / "save.txt"
    if path.is_file():
        with path.open() as f:
            return {ln.strip() for ln in f if ln.strip()}
    return set()

def save_new_hashes(matches: list, existing: set) -> None:
    """Append hashes of newly‑saved entries to save.txt."""
    path = SCRIPT_DIR / "save.txt"
    with path.open("a") as f:
        for entry, _ in matches:
            h = title_hash(entry.title)
            if h not in existing:
                f.write(f"{h}\n")
    log.info("Saved %d new hashes to %s", len(matches), path)

def fuzzy_match(author: str, entry_authors: list, thresh: int = 80) -> bool:
    return any(fuzz.token_sort_ratio(author, a) >= thresh for a in entry_authors)

def print_aligned(entry, authors):
    lines = [
        f"Title:       {entry.title}",
        f"Link:        {entry.link}",
        f"Published:   {entry.published}",
        f"Authors:     {', '.join(authors)}",
    ]
    max_len = max(len(l.split(":")[0]) for l in lines) + 1
    fmt = f"{{:<{max_len}}} {{}}"
    for line in lines:
        key, val = line.split(":", 1)
        log.info(fmt.format(key + ":", val.strip()))
    log.info("")

def return_pdf_links(matches) -> list:
    pdf_links = [entry.link.rstrip("/") + ".pdf" for entry, _ in matches]
    log.debug("Returning %d PDF links for post‑email processing", len(pdf_links))
    return pdf_links

# ----------------------------------------------------------------------
# ── ZOTERO HELPERS ----------------------------------------------------
# ----------------------------------------------------------------------
def get_zotero_client():
    uid, key = get_zotero_credentials()
    return zotero.Zotero(uid, "user", key)

def ensure_collection(zot, name: str) -> str:
    """
    Return the collection key for *name*.  Create the collection if it does not exist.
    """
    collections = zot.collections()
    for coll in collections:
        if coll.get("data", {}).get("name") == name:
            return coll["data"]["key"]
    # Not found → create it
    payload = [{"name": name}]
    created = zot.create_collections(payload)
    if not created:
        raise RuntimeError(f"Failed to create Zotero collection '{name}'")
    collections = zot.collections()
    for coll in collections:
        if coll.get("data", {}).get("name") == name:
            log.info("Created Zotero collection '%s' (key=%s)", name, new_key)
            return coll["data"]["key"]
    return new_key

def download_pdf(entry) -> Path | None:
    """
    Download the PDF for a feed entry into ``SCRIPT_DIR/tmp_pdfs``.
    Returns the absolute ``Path`` of the saved file, or ``None`` if the
    download fails or the URL does not point to a PDF.
    """
    pdf_url = entry.link.rstrip("/") + ".pdf"

    # Derive a safe filename from the title – replace filesystem‑unsafe chars
    safe_title = "".join(ch if ch.isalnum() or ch in " ._-()" else "_" for ch in entry.title)
    filename = f"{safe_title}.pdf"
    dest_path = TMP_PDF_DIR / filename

    try:
        # Stream the response so we don’t hold the whole file in memory
        with requests.get(pdf_url, timeout=20, stream=True) as r:
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "").lower()
            if "pdf" not in ct:
                log.warning("URL %s returned non‑PDF content type %s", pdf_url, ct)
                return None

            # Write to disk chunk‑by‑chunk
            with dest_path.open("wb") as out_f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:               # filter out keep‑alive chunks
                        out_f.write(chunk)

        log.info("✅ PDF saved to %s", dest_path)
        return dest_path

    except Exception as exc:                     # includes HTTP errors, timeouts, etc.
        log.error("Failed to fetch PDF from %s: %s", pdf_url, exc)
        # Clean up a partially written file, if any
        if dest_path.exists():
            try:
                dest_path.unlink()
            except Exception:
                pass
        return None

def get_existing_items(zot, collection_key):
    """
    Returns a dict mapping a normalized identifier → Zotero item key.
    By default we index by lower‑cased title, but you can also add DOI/arXiv.
    """
    existing = {}
    # Zotero paginates results; 100 is the max per page.
    start = 0
    while True:
        items = zot.collection_items(collection_key, limit=100, start=start)
        if not items:
            break
        for it in items:
            data = it.get("data", {})
            # Normalise the title for quick look‑ups
            title_norm = data.get("title", "").strip().lower()
            if title_norm:
                existing[title_norm] = it["key"]

            # Optional: also index by DOI or arXiv ID if present
            doi = data.get("DOI")
            if doi:
                existing[doi.lower()] = it["key"]
            # Example for arXiv: store the arXiv identifier if you keep it in extra
            extra = data.get("extra", "")
            for line in extra.splitlines():
                if line.lower().startswith("arxiv:"):
                    arxiv_id = line.split(":", 1)[1].strip().lower()
                    existing[arxiv_id] = it["key"]
        start += len(items)
    return existing

def upload_entry_to_zotero(entry, collection_key: str) -> None:
    """
    Creates a Zotero item only if it isn’t already present in the collection.
    Duplicates are detected by title (case‑insensitive) and, if available,
    by DOI or arXiv ID.
    """
    zot = get_zotero_client()

    existing_index = get_existing_items(zot, collection_key)

    creators = []
    for name in [a.get("name", "").strip() for a in entry.get("authors", [])]:
        parts = name.rsplit(" ", 1)
        first, last = ("", parts[0]) if len(parts) == 1 else (parts[0], parts[1])
        creators.append({"creatorType": "author", "firstName": first, "lastName": last})

    # Normalised title for comparison
    title_norm = entry.title.strip().lower()

    # Try to pull a DOI or arXiv ID from the feed entry if it exists
    # (adjust the attribute names according to the actual feed structure)
    doi = getattr(entry, "doi", None)
    arxiv = getattr(entry, "arxiv_id", None)

    duplicate_key = None
    if title_norm in existing_index:
        duplicate_key = existing_index[title_norm]
    elif doi and doi.lower() in existing_index:
        duplicate_key = existing_index[doi.lower()]
    elif arxiv and arxiv.lower() in existing_index:
        duplicate_key = existing_index[arxiv.lower()]

    if duplicate_key:
        log.info(
            "⏭️ Skipping duplicate – item already in collection (key=%s, title=%s)",
            duplicate_key,
            entry.title,
        )
        return  # Nothing else to do

    item = {
        "itemType": "journalArticle",
        "title": entry.title,
        "url": entry.link,
        "date": entry.published,
        "creators": creators,
        "collections": [collection_key],
    }

    # If you have a DOI or arXiv ID you want to store, add it now:
    if doi:
        item["DOI"] = doi
    if arxiv:
        # Storing arXiv ID in the “extra” field is common practice
        item["extra"] = f"arXiv:{arxiv}"

    created = zot.create_items([item])
    if not created:
        raise RuntimeError("Zotero item creation failed")

    item_key = created["successful"]["0"]["key"]
    log.info("✅ Created Zotero item %s (collection %s)", item_key, collection_key)

    pdf_path = download_pdf(entry)
    attach_result = zot.attachment_simple(
        files=[str(pdf_path)],      # convert Path → str for the API
        parentid=item_key
    )

    if attach_result["failure"] == []:
        logging.info("📎 Attached (attachment key %s)", item_key)
        pdf_path.unlink(missing_ok=True)

    else:
        logging.warning("❌ Failed to attach %s – response: %s", fname, info)

# ----------------------------------------------------------------------
# ── EMAIL HELPERS -----------------------------------------------------
# ----------------------------------------------------------------------
def build_email_body(matches: list) -> str:
    """
    Returns a plain‑text body that lists each match *and* the direct PDF link.
    """
    parts = ["New IACR papers matching your author list:\n"]
    for entry, authors in matches:
        pdf_link = entry.link.rstrip("/") + ".pdf"
        parts.append(f"Title   : {entry.title}")
        parts.append(f"Link    : {entry.link}")
        parts.append(f"PDF Link: {pdf_link}")
        parts.append(f"Date    : {entry.published}")
        parts.append(f"Authors : {', '.join(authors)}")
        parts.append("")   # blank line between entries
    return "\n".join(parts)

def send_email(matches: list) -> None:
    """
    Sends the digest via mailgun and returns a list of the **PDF URLs** that were
    included in the email.  The caller can then process those URLs
    independently (e.g. upload to Zotero).
    """
    if not matches:
        log.info("No matches – skipping email.")
        return []

    body = ""
    body = build_email_body(matches)

    recipient = get_recipient()
    if not recipient:
        log.warning("Recipient address not set – email not sent.")
        return []

    # get environment variables
    domain_id = get_mailgun_domain()

    if not domain_id:
        log.warning("Domain not set – email not sent.")
        return []
    api_key  = get_mailgun_api_key()

    if not api_key:
        log.warning("API key not set – email not sent.")
        return []

    domain = f'https://api.mailgun.net/v3/{domain_id}.mailgun.org/messages'
    sender = f'Mailgun Sandbox <postmaster@{domain_id}.mailgun.org>'

    data = {
        "from": sender,
        "to": recipient,
        "subject": "New papers from your authors of interest!",
        "text": body,
    }

    try:
        resp = requests.post(domain, auth=("api", api_key), data=data, timeout=15)
        resp.raise_for_status()
        log.info("Email sent successfully! (%s)", resp.text[:120])
    except requests.RequestException as exc:
        log.error("Failed to send email: %s", exc)



# ----------------------------------------------------------------------
# ── ZOTERO UPLOADER ---------------------------------------------------
# ----------------------------------------------------------------------
def upload_pdfs_to_zotero(pdf_urls: list, matches: list) -> None:
    """
    Given the PDF URLs that were mailed and the original `matches`,
    download each PDF, create a Zotero item and attach the file.
    All items are placed in the collection named ``ZOTERO_COLLECTION_NAME``.
    """
    if not pdf_urls:
        log.info("No PDF URLs to process for Zotero – exiting uploader.")
        return

    zot = get_zotero_client()
    collection_key = ensure_collection(zot, ZOTERO_COLLECTION_NAME)

    # Build a quick lookup from PDF URL → original feed entry (needed for metadata)
    url_to_entry = {e.link.rstrip("/") + ".pdf": e for e, _ in matches}

    for pdf_url in pdf_urls:
        entry = url_to_entry.get(pdf_url)
        if not entry:
            log.warning("Could not locate feed entry for PDF URL %s – skipping", pdf_url)
            continue

        upload_entry_to_zotero(entry, collection_key)


# ----------------------------------------------------------------------
# ── MAIN FEED PROCESSING ----------------------------------------------
# ----------------------------------------------------------------------
def fetch_and_process(feed_url: str, authors_of_interest: list) -> None:
    log.debug("Fetching feed from %s", feed_url)
    feed = feedparser.parse(feed_url)

    if feed.bozo:
        log.error("Feed parsing error: %s", feed.bozo_exception)
        return

    log.info("Found %d entries in the feed", len(feed.entries))
    matches = []

    for entry in feed.entries:
        # ---- sanity checks -------------------------------------------------
        if "published_parsed" not in entry:
            log.debug("Skipping entry without published date: %s", entry.title)
            continue
        entry_dt = datetime.datetime.fromtimestamp(time.mktime(entry.published_parsed))

        # ---- author extraction ---------------------------------------------
        entry_authors = [a.get("name", "").strip() for a in entry.get("authors", [])]

        # ---- duplicate / time‑window filtering ------------------------------
        h = title_hash(entry.title)
        existing_hashes = read_existing_hashes()
        if h in existing_hashes:
            log.debug("Duplicate (already seen): %s", entry.title)
            continue

        # ---- fuzzy author match --------------------------------------------
        if any(fuzzy_match(a, entry_authors) for a in authors_of_interest):
            matches.append((entry, tuple(entry_authors)))
            print_aligned(entry, entry_authors)

    send_email(matches)
    pdf_urls = return_pdf_links(matches)
    upload_pdfs_to_zotero(pdf_urls, matches)
    save_new_hashes(matches, existing_hashes)

# ----------------------------------------------------------------------
# ── ENTRY POINT -------------------------------------------------------
# ----------------------------------------------------------------------
if __name__ == "__main__":
    args = parse_cli()
    log = configure_logger(args.debug)

    # ---- load author list ------------------------------------------------
    authors_path = SCRIPT_DIR / "authors.txt"
    if not authors_path.is_file():
        log.error("Missing authors.txt at %s", authors_path)
        exit(1)

    with authors_path.open() as f:
        authors_of_interest = [ln.strip() for ln in f if ln.strip()]

    log.info("Monitoring %d authors", len(authors_of_interest))
    fetch_and_process(FEED_URL, authors_of_interest)
