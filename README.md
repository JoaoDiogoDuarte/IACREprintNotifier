# Saucy ePrint notifier

Get IACR ePrint notifications from authors you care about by email and Zotero. This is done via RSS parsing, hashing and using some free email sending service.
I made this because I could not find a tool that suited my needs.

## Disclaimer on AI generated code

I relied on ChatGPT and Proton Lumo for large amounts of code generation as I have better things to do (i.e. actually reading the papers) than writing a tool like this by hand.
This seemed like reasonable approach as this code isn't critical nor production level, and the task itself is non-essential shit-work (see https://zachholman.com/posts/shit-work/). 
It's more of a small quality of life enhancement.

## How to get this to work:

1. Python 3.whocares (joking aside, at least 3.7 and use a virtual env for your sanity)
2. A Mailgun account (https://www.mailgun.com/ free if you only send emails to yourself)
3. A Zotero account (https://www.zotero.org/), and an API key and API User ID (see https://www.zotero.org/settings/security#applications) 
4. Install requirements (use `pip install -r requirements.txt`)
5. Edit the domain, API key, from and to email addresses in `eprint.py` *or* store them in your environment (see code for environment variable names)
6. Repeat above, but for the Zotero API key and user ID
7. Edit `authors.txt` to include the authors you want to get updates from
8. Make sure `save.txt` is initially clean: the program hashes the data associated with the new publication and store it in `save.txt` to avoid sending duplicate papers
9. Have some timer run `/path/to/.env/bin/python eprint.py` to get notifications by email (will end up in spam, so flag it as not spam). They will look like this:

```
New IACR papers matching your author list:

Title:   Simple threshold decryption secure against adaptive corruptions
Link:    https://eprint.iacr.org/2025/1578
Date:    Tue, 02 Sep 2025 17:25:21 +0000
Authors: Victor Shoup

Title:   How Hard Can It Be to Formalize a Proof? Lessons from Formalizing CryptoBox Three Times in EasyCrypt
Link:    https://eprint.iacr.org/2025/1569
Date:    Tue, 02 Sep 2025 05:49:10 +0000
Authors: François Dupressoir, Andreas Hülsing, Cameron Low, Matthias Meijers, Charlotte Mylog, Sabine Oechsner

---
Title:   Formally Verified Correctness Bounds for Lattice-Based Cryptography
Link:    https://eprint.iacr.org/2025/1562
Date:    Sun, 31 Aug 2025 12:59:04 +0000
Authors: Manuel Barbosa, Matthias J. Kannwischer, Thing-han Lim, Peter Schwabe, Pierre-Yves Strub

```

Technically you can either have just a Zotero or just a Mailgun account and comment out the appropriate line(s) of code.
You do you.

### Note on the timer 

Note, I use `anacron` from the `cronie` package such that my user `crontab` looks like this (runs `anacron` every hour):

```cron
0 * * * *  /usr/sbin/anacron -s -t "${HOME}/.local/etc/anacrontab" -S "${HOME}/.local/var/spool/anacron"
```

and my user `anacrontab` (stored in `~/.local/etc/anacrontab` looks like this:

```cron
SHELL=/bin/sh
PATH=/sbin:/bin:/usr/sbin:/usr/bin
MAILTO=joao
# the maximal random delay added to the base delay of the jobs
RANDOM_DELAY=45
# the jobs will be started during the following hours only
START_HOURS_RANGE=3-22

1  0  cron.mine    run-parts /home/joao/.local/etc/cron.daily/
```

and my `/home/joao/.local/etc/cron.daily/` contains, amongst others, a simple executable file called `eprint`:

```bash
#!/bin/bash
/home/joao/.pyenv/shims/python /home/joao/Work/personal/eprintupdates/eprint.py
```

In the end, `cron` is checking every hour if `anacron` has run today. If it has not, `anacron` will run, and if not, nothing happens. 
You could use `systemd-timer` but it just did not work for me and I got tired of trying to understand why and produce a fix.
