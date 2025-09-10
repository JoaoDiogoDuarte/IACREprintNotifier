#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import logging
import os
import time
from pathlib import Path

import feedparser
import requests
from fuzzywuzzy import fuzz

# ----------------------------------------------------------------------
# Configuration (unchanged)
feed_url = "https://eprint.iacr.org/rss/rss.xml"
script_dir = Path(__file__).resolve().parent

# ----------------------------------------------------------------------
# Argument parsing & logger setup
def parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch the IACR RSS feed for papers by a set of authors."
    )
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug output")
    return parser.parse_args()


def configure_logger(debug: bool) -> logging.Logger:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        level=level,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Helper functions (now using `log`)
def hours_ago(in_hours_ago: int = 24) -> float:
    delta = datetime.timedelta(hours=in_hours_ago)
    ts = time.time() - delta.total_seconds()
    log.debug(f"Cut‑off timestamp (UTC) = {ts}")
    return ts


def generate_title_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8")).hexdigest()


def read_existing_hashes() -> set:
    path = script_dir / "save.txt"
    if path.is_file():
        with path.open("r") as f:
            hashes = {ln.strip() for ln in f if ln.strip()}
        log.debug(f"Loaded {len(hashes)} hashes from {path}")
        return hashes
    log.debug("No save.txt found – starting fresh")
    return set()


def save_matches_to_file(matches: list, existing_hashes: set) -> None:
    path = script_dir / "save.txt"
    with path.open("a") as f:
        for entry, _ in matches:
            h = generate_title_hash(entry.title)
            if h not in existing_hashes:
                f.write(f"{h}\n")
    log.info("New matches saved to %s.", path)
    log.info("")  # blank line



def is_fuzzy_match(author: str, item_authors: list, threshold: int = 80) -> bool:
    for ia in item_authors:
        score = fuzz.token_sort_ratio(author, ia)
        log.debug(f"Fuzzy score {author!r} vs {ia!r}: {score}")
        if score >= threshold:
            return True
    return False


def print_aligned_item(entry, item_authors) -> None:
    title = f"Title:   {entry.title}"
    link = f"Link:    {entry.link}"
    published = f"Published:    {entry.published}"
    authors = "Authors: " + ", ".join(item_authors)

    max_len = max(len("Title:   "), len("Link:    "), len("Published:    "), len("Authors: "))
    fmt = f"{{:<{max_len}}}"
    log.info(fmt.format(title))
    log.info(fmt.format(link))
    log.info(fmt.format(published))
    log.info(fmt.format(authors))
    log.info("")  # blank line


def send_email(matches: list) -> None:
    if not matches:
        log.info("No matches – skipping email.")
        log.info("")  # blank line
        return

    body = ""
    for entry, item_authors in matches:
        body += f"""Title:   {entry.title}
Link:    {entry.link}
Date:    {entry.published}
Authors: {', '.join(item_authors)}

---\n"""

    # get environment variables
    domain_id   = os.getenv("MG_DOMAIN")
    api_key  = os.getenv("MG_API_KEY")
    to_email   = os.getenv("MG_TO")
    
    log.info("OS environments found:")
    log.info(f"\tDomain ID: {domain_id}")
    log.info(f"\tAPI key: {api_key}")
    log.info(f"\tTo email: {to_email}")
    log.info("")  # blank line

    
    # or optionally, just hardcode everything
    missing = [var for var, val in {
        "MG_DOMAIN": domain_id,
        "MG_API_KEY": api_key,
        "MG_TO": to_email,
    }.items() if not val]

    if missing:
        domain_id = "mydomain"
        api_key = "myapikey"
        to_email = "Joe Doe <joe@doe.bro>"
        log.info("OS environments empty!")
        log.info(f"\tDomain ID: {domain_id}")
        log.info(f"\tAPI key: {api_key}")
        log.info(f"\tTo email: {to_email}")
        log.info("")  # blank line

    domain = f'https://api.mailgun.net/v3/{domain_id}.mailgun.org/messages'
    from_email = f'Mailgun Sandbox <postmaster@{domain_id}.mailgun.org>'
    
    data = {
        "from": from_email,
        "to": to_email,
        "subject": "New papers from your authors of interest!",
        "text": body,
    }

    try:
        resp = requests.post(domain, auth=("api", api_key), data=data, timeout=15)
        resp.raise_for_status()
        log.info("Email sent successfully! (%s)", resp.text[:120])
    except requests.RequestException as exc:
        log.error("Failed to send email: %s", exc)


def fetch_and_filter_feed(feed_url: str, authors_of_interest: list) -> None:
    log.debug(f"Downloading feed from {feed_url}")
    feed = feedparser.parse(feed_url)

    if feed.bozo:
        log.error(f"Feed parsing error: {feed.bozo_exception}")
        return

    log.info(f"Retrieved {len(feed.entries)} entries")
    matches = []
    cutoff_ts = hours_ago()
    cutoff_dt = datetime.datetime.fromtimestamp(cutoff_ts)

    existing_hashes = read_existing_hashes()

    for entry in feed.entries:
        # Published date handling – some feeds omit it, guard against KeyError
        if "published_parsed" not in entry:
            log.debug(f"Entry missing published date: {entry.title}")
            continue
        entry_dt = datetime.datetime.fromtimestamp(time.mktime(entry.published_parsed))

        # Author extraction – feedparser gives a list of dicts
        item_authors = [a.get("name", "").strip() for a in entry.get("authors", [])]

        title_hash = generate_title_hash(entry.title)
        if title_hash in existing_hashes:
            log.debug(f"Skipping duplicate: {entry.title}")
            continue

        # Time window check
        if entry_dt > cutoff_dt:
            log.debug(f"Skipping too‑new entry ({entry_dt.isoformat()}): {entry.title}")
            continue

        # Author match (fuzzy)
        if any(is_fuzzy_match(a, item_authors) for a in authors_of_interest):
            matches.append((entry, tuple(item_authors)))
            print_aligned_item(entry, item_authors)

    if matches:
        save_matches_to_file(matches, existing_hashes)
        send_email(matches)
    else:
        log.info("No new matching papers found.")


# ----------------------------------------------------------------------
if __name__ == "__main__":
    args = parse_cli()
    log = configure_logger(args.debug)

    # Load authors list
    authors_path = script_dir / "authors.txt"
    if not authors_path.is_file():
        log.error("Missing authors.txt at %s", authors_path)
        exit(1)

    with authors_path.open("r") as f:
        authors_of_interest = [ln.strip() for ln in f if ln.strip()]

    log.info("Monitoring %d authors", len(authors_of_interest))
    fetch_and_filter_feed(feed_url, authors_of_interest)
