# Saucy ePrint notifier

Get an email when one of your favourite authors posts to IACR ePrint. Parses the ePrint RSS
feed, fuzzy-matches authors against a list you control, and mails you a digest of anything new.

## Disclaimer on AI generated code

I relied on LLMs for large amounts of code generation as I have better things to do (i.e. actually
reading the papers) than writing a tool like this by hand. This seemed like a reasonable approach as
this code isn't critical nor production level, and the task itself is non-essential shit-work (see
https://zachholman.com/posts/shit-work/). It's more of a small quality of life enhancement.

## How it works

1. Fetch `https://eprint.iacr.org/rss/rss.xml` (the 100 most recent papers).
2. Skip anything whose ePrint id (e.g. `2026/1693`) is already in `save.txt`.
3. Keep papers with an author matching a line in `authors.txt`.
4. Mail the digest via Mailgun.
5. **Only then** record those ids in `save.txt`. A failed send means the papers are retried on the
   next run rather than silently lost.

Matching requires the surnames to be equal after accent/case stripping, then scores the full names
with `rapidfuzz` at a threshold of 90. So `Andreas Hülsing` matches `Andreas Hulsing`, but
`Peter Schwabe` will not swallow `Peter Schweizer`.

## Setup

### 1. Mailgun

Create a Mailgun account (https://www.mailgun.com/, free tier is fine for mailing yourself). You
need the API key and the sending domain. On a sandbox domain you must add your address under
*Authorized Recipients* first, and mail will land in spam until you flag it as not spam.

Configuration is read from the environment only, there are no hard-coded credentials:

| Variable | Required | Meaning |
| --- | --- | --- |
| `MG_API_KEY` | yes | Mailgun private API key |
| `MG_DOMAIN` | yes | Full sending domain, e.g. `sandbox123.mailgun.org` |
| `MG_TO` | yes | Your address |
| `MG_FROM` | no | Defaults to `postmaster@$MG_DOMAIN` |
| `MG_BASE_URL` | no | Defaults to `https://api.mailgun.net/v3`, set to the `api.eu` host for EU accounts |

### 2. Authors

Edit `authors.txt`, one name per line. Duplicates are ignored.

### 3. Local run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python eprint.py --dry-run     # print the digest, change nothing
.venv/bin/python eprint.py --seed        # mark everything currently matching as seen, send nothing
.venv/bin/python eprint.py               # for real
```

Run `--seed` once before the first real run, otherwise you get a digest of every matching paper
still in the feed window.

### 4. Scheduled runs on GitHub Actions

`.github/workflows/eprint.yml` runs daily at 07:00 UTC and commits `save.txt` back to the repo, so
state survives between runs. This repo's origin is Codeberg, so add GitHub as a second remote:

```bash
git remote add github git@github.com:YOURNAME/eprintupdates.git
git push github main
```

Then in the GitHub repo under *Settings → Secrets and variables → Actions*, add `MG_API_KEY`,
`MG_DOMAIN`, `MG_TO`, and optionally `MG_FROM`. Trigger a first run by hand from the *Actions* tab
(*Run workflow*) to check it works.

Two things to know about this setup:

- Pushing to Codeberg no longer updates the runner. Push to both remotes, or set Codeberg's origin
  to push to GitHub as well.
- GitHub disables scheduled workflows after 60 days of repository inactivity. It warns by email
  first, and re-enabling is one click in the Actions tab.

### Alternative: local cron

The script is a plain CLI, so `anacron` still works. I use `cronie` with a user `crontab` that runs
`anacron` hourly:

```cron
0 * * * *  /usr/sbin/anacron -s -t "${HOME}/.local/etc/anacrontab" -S "${HOME}/.local/var/spool/anacron"
```

with `~/.local/etc/anacrontab`:

```cron
SHELL=/bin/sh
PATH=/sbin:/bin:/usr/sbin:/usr/bin
MAILTO=joao
RANDOM_DELAY=45
START_HOURS_RANGE=3-22

1  0  cron.mine    run-parts /home/joao/.local/etc/cron.daily/
```

and an executable `~/.local/etc/cron.daily/eprint` that exports the `MG_*` variables and runs the
script. `cron` checks hourly whether `anacron` has run today; if not, it runs.

## Email format

```
New IACR ePrint papers matching your author list:

Title   : The ePrint:2026/1591 Quantum Algorithm Does Not Solve DCP
Link    : https://eprint.iacr.org/2026/1693
PDF     : https://eprint.iacr.org/2026/1693.pdf
Date    : Sat, 15 Aug 2026 03:46:13 +0000
Authors : Aparna Gupte, Seyoon Ragavan, Mark Zhandry
Matched : Mark Zhandry
```

## Known limits

The RSS feed only carries the 100 most recent papers. If nothing runs for long enough that more
than 100 papers are posted, the missed ones are gone for good and you are never told. A daily run
has a wide margin; a weekly one does not.
