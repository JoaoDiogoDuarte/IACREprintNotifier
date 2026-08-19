#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Watch the IACR ePrint RSS feed and email a digest of papers by authors of interest."""

import argparse
import logging
import os
import re
import sys
import unicodedata
from pathlib import Path

import feedparser
import requests
from rapidfuzz import fuzz

FEED_URL = "https://eprint.iacr.org/rss/rss.xml"
SCRIPT_DIR = Path(__file__).resolve().parent
AUTHORS_PATH = SCRIPT_DIR / "authors.txt"
STATE_PATH = SCRIPT_DIR / "save.txt"

# Fuzzy-match threshold for author names (0-100).
MATCH_THRESHOLD = 90

log = logging.getLogger("eprint")


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
def mailgun_config() -> dict:
    """Read Mailgun settings from the environment. Missing values are returned as None."""
    return {
        "api_key": os.getenv("MG_API_KEY"),
        "domain": os.getenv("MG_DOMAIN"),
        "recipient": os.getenv("MG_TO"),
        "sender": os.getenv("MG_FROM"),
        "base_url": os.getenv("MG_BASE_URL", "https://api.mailgun.net/v3"),
    }


# ----------------------------------------------------------------------
# State: which papers have already been mailed
# ----------------------------------------------------------------------
def eprint_id(entry) -> str | None:
    """Extract the stable ePrint identifier, e.g. '2025/1578', from an entry link."""
    match = re.search(r"/(\d{4}/\d+)", entry.link)
    return match.group(1) if match else None


def read_seen_ids() -> set:
    if not STATE_PATH.is_file():
        return set()
    with STATE_PATH.open() as f:
        return {line.strip() for line in f if line.strip()}


def append_seen_ids(ids) -> None:
    """Append ids to the state file, keeping it sorted and free of duplicates."""
    combined = sorted(read_seen_ids() | set(ids))
    tmp_path = STATE_PATH.with_suffix(".tmp")
    tmp_path.write_text("\n".join(combined) + "\n")
    tmp_path.replace(STATE_PATH)
    log.info("Recorded %d new paper(s); state now holds %d.", len(set(ids)), len(combined))


# ----------------------------------------------------------------------
# Author matching
# ----------------------------------------------------------------------
def normalise(name: str) -> str:
    """Strip accents, punctuation and case so that 'Hülsing' matches 'Hulsing'."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z ]", " ", stripped.lower()).strip()


def surname(name: str) -> str:
    parts = normalise(name).split()
    return parts[-1] if parts else ""


def is_match(wanted: str, candidate: str) -> bool:
    """A candidate matches when the surnames agree and the full names are close enough."""
    if surname(wanted) != surname(candidate):
        return False
    return fuzz.token_sort_ratio(normalise(wanted), normalise(candidate)) >= MATCH_THRESHOLD


def matching_authors(entry_authors: list, authors_of_interest: list) -> list:
    """Return the authors of interest that appear on this paper."""
    return [w for w in authors_of_interest if any(is_match(w, a) for a in entry_authors)]


def load_authors() -> list:
    if not AUTHORS_PATH.is_file():
        log.error("Missing authors.txt at %s", AUTHORS_PATH)
        sys.exit(1)

    seen, authors = set(), []
    with AUTHORS_PATH.open() as f:
        for line in f:
            name = line.strip()
            key = normalise(name)
            if name and key not in seen:
                seen.add(key)
                authors.append(name)
    return authors


# ----------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------
def build_email_body(matches: list) -> str:
    parts = ["New IACR ePrint papers matching your author list:\n"]
    for entry, hits in matches:
        parts.append(f"Title   : {entry.title}")
        parts.append(f"Link    : {entry.link}")
        parts.append(f"PDF     : {entry.link.rstrip('/')}.pdf")
        parts.append(f"Date    : {entry.published}")
        parts.append(f"Authors : {', '.join(a.get('name', '').strip() for a in entry.get('authors', []))}")
        parts.append(f"Matched : {', '.join(hits)}")
        parts.append("")
    return "\n".join(parts)


def send_email(matches: list) -> bool:
    """Send the digest. Returns True only if Mailgun accepted the message."""
    cfg = mailgun_config()
    missing = [k for k in ("api_key", "domain", "recipient") if not cfg[k]]
    if missing:
        log.error("Mailgun not configured, missing: %s", ", ".join(missing))
        return False

    sender = cfg["sender"] or f"ePrint Notifier <postmaster@{cfg['domain']}>"
    subject = f"{len(matches)} new ePrint paper(s) from your authors"

    try:
        resp = requests.post(
            f"{cfg['base_url']}/{cfg['domain']}/messages",
            auth=("api", cfg["api_key"]),
            data={
                "from": sender,
                "to": cfg["recipient"],
                "subject": subject,
                "text": build_email_body(matches),
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        # Mailgun explains refusals in the response body, so surface it.
        detail = ""
        if exc.response is not None:
            detail = f" -- Mailgun said: {exc.response.text[:500]}"
        log.error("Failed to send email: %s%s", exc, detail)
        return False

    log.info("Email sent to %s", cfg["recipient"])
    return True


# ----------------------------------------------------------------------
# Feed processing
# ----------------------------------------------------------------------
def fetch_matches(feed_url: str, authors_of_interest: list) -> list:
    """Return [(entry, matched_authors)] for unseen feed entries by authors of interest."""
    log.debug("Fetching feed from %s", feed_url)
    feed = feedparser.parse(feed_url)

    if not feed.entries:
        log.error("No entries in feed (%s)", getattr(feed, "bozo_exception", "empty response"))
        return []
    if feed.bozo:
        log.warning("Feed reported a parse issue but returned entries: %s", feed.bozo_exception)

    log.info("Found %d entries in the feed", len(feed.entries))
    seen_ids = read_seen_ids()
    matches = []

    for entry in feed.entries:
        paper_id = eprint_id(entry)
        if not paper_id:
            log.warning("Could not extract an ePrint id from %s, skipping", entry.link)
            continue
        if paper_id in seen_ids:
            log.debug("Already seen: %s", paper_id)
            continue

        entry_authors = [a.get("name", "").strip() for a in entry.get("authors", [])]
        hits = matching_authors(entry_authors, authors_of_interest)
        if hits:
            log.info("Match %s: %s (%s)", paper_id, entry.title, ", ".join(hits))
            matches.append((entry, hits))

    return matches


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Watch the IACR ePrint RSS feed for papers by a set of authors."
    )
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug output")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Record every current match as seen without sending email (run once when migrating).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest instead of emailing it, and leave the state file untouched.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        level=logging.DEBUG if args.debug else logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    authors_of_interest = load_authors()
    log.info("Monitoring %d authors", len(authors_of_interest))

    matches = fetch_matches(FEED_URL, authors_of_interest)
    if not matches:
        log.info("No new papers.")
        return 0

    ids = [eprint_id(entry) for entry, _ in matches]

    if args.seed:
        append_seen_ids(ids)
        log.info("Seeded state with %d paper(s), no email sent.", len(ids))
        return 0

    if args.dry_run:
        print(build_email_body(matches))
        return 0

    # Only record papers as seen once the email has actually gone out, so a
    # delivery failure means they are retried on the next run rather than lost.
    if not send_email(matches):
        return 1

    append_seen_ids(ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
