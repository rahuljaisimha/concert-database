# Where to run it

## What the job needs

- **Compute:** almost nothing. One run a day takes ~30–40 minutes, mostly
  waiting between requests (setlist.fm: 1,400 requests ~1 s apart, slower
  after a rate-limit response).
- **Memory:** well under 1 GB.
- **Disk:** a few GB over the first year (database + compressed raw archive).
- **Uptime:** it has to run *once a day*, not continuously. The optional
  Gemini step (`concertdb gemini`) runs for a few more hours a day, alongside
  the crawl.

## What happens if the machine is off

Everything resumes from the database. Nothing is lost, only delayed:

| Off for | Effect |
|---|---|
| A crash mid-run | Each page is committed as it's stored; at most one page is redone. |
| Hours to a few days | Nothing. The timer runs as soon as the machine is back (`Persistent=true`), and every unit of work picks up from its cursor. setlist.fm progress is just a few days slower. |
| Weeks | The backfill is slower by that much. The real cost is upcoming-show capture: a small venue's listing may vanish after the show, so bills for shows that happen while the machine is off can be missed. Those become gaps for the search step to fill later. |
| Disk dies | Restore from R2 (`concertdb restore`); you lose at most the work since the last daily backup. |

So the tolerance is generous: aim for "on most days", not 24/7.

## Options

| Option | Cost | For | Against |
|---|---|---|---|
| **Raspberry Pi 5 (4 GB) + USB SSD** | ~$100–130 once, a few $/year power | Home internet = residential IP, which venue and ticketing sites block far less than cloud IPs. Silent, always on. | Needs an SSD (SD cards wear out under database writes). Home power/internet outages (fine, see above). |
| A computer you already own that stays on (old laptop, Mac mini, NAS) | $0 | Same residential-IP advantage | Must stay awake; laptops sleeping skip days (still fine, just slower). |
| Hetzner / similar small VPS | ~$4–6/month | Reliable, cheap, simple Linux box | Datacenter IP: more sites block scraping. |
| AWS EC2 `t4g.micro` | ~$6–8/month (+ storage) | Reliable; you may already use AWS | More expensive and more setup (security groups, EBS) than a VPS for the same job; datacenter IP. |
| GitHub Actions cron | $0 (public repo) | No machine at all | No disk: must download and re-upload the database from R2 every run; logs are public; datacenter IP. Reasonable fallback, not the primary. |

Prices are rough; check current pricing.

## Recommendation

**Use a computer you already leave on.** A Windows PC that's on almost all
the time is a good fit: home IP, no purchase, and missed days are caught up
automatically. See "Windows setup" below. Otherwise:

**A Raspberry Pi 5 with a small USB SSD**. The residential IP matters more than reliability here: the
capture step reads venue and ticketing pages, and those are the sites most
likely to block cloud servers. R2 backups make the hardware disposable.

Skip EC2 unless you specifically want it on AWS; a ~$5 VPS does the same job
for less. If you'd rather not own hardware, start with a VPS and move to a Pi
later; `concertdb restore` makes the move a five-minute job.

## Pi setup sketch

1. Raspberry Pi OS Lite (64-bit), boot from (or mount) the SSD.
2. `sudo apt install python3-venv git`, then follow Setup in the README.
3. Put `.env` in the repo directory (`chmod 600 .env`).
4. Install both timers (instructions at the top of `deploy/concertdb.service`
   and `deploy/concertdb-pollstar.service`).
5. `systemctl --user list-timers` to confirm; `journalctl --user -u concertdb -u concertdb-pollstar` for logs.

## Windows setup (WSL)

The crawler runs inside WSL (Windows Subsystem for Linux), started daily by
Windows Task Scheduler.

1. In an admin PowerShell: `wsl --install -d Ubuntu`, reboot, create the
   Linux user when Ubuntu opens.
2. In Ubuntu (keep the repo in the Linux home, not under `/mnt/c`, which is
   much slower for SQLite):
   ```bash
   sudo apt update && sudo apt install -y python3-venv git
   cd ~ && git clone https://github.com/rahuljaisimha/concert-database && cd concert-database
   python3 -m venv .venv && .venv/bin/pip install -e '.[r2]'
   cp .env.example .env && chmod 600 .env && nano .env    # paste the secrets
   ```
3. Load the artists and test one cycle by hand:
   ```bash
   .venv/bin/concertdb seed          # downloads MusicSceneMap's graph.db.gz
   .venv/bin/concertdb prioritize "Osees" "Flat Worms"
   deploy/run.sh && tail -30 data/run.log
   ```
4. In normal PowerShell, from the repo folder (`\\wsl$\Ubuntu\home\<you>\concert-database`):
   `.\deploy\windows-task.ps1 -Distro Ubuntu` (the distribution's name from
   `wsl -l -v`). It registers two daily 4 am tasks that also run when the PC
   comes back after missing 4 am: `concert-database` (`deploy/run.sh`, about
   an hour, log in `data/run.log`) and `concert-database-pollstar`
   (`deploy/run-pollstar.sh`, Pollstar at ~1 request/s for several hours, log
   in `data/pollstar.log`). They run side by side; see "Concurrency" in
   docs/ARCHITECTURE.md.
   A third task, `concert-database-gemini`, starts at 1 am (`deploy/gemini.sh`,
   log in `data/gemini.log`; it exits quickly when `GEMINI_API_KEY` isn't set).
   The Gemini step's budget follows the Pacific day whatever the PC's time
   zone, so the 1 am start only matters for using a fresh quota early.
5. Power options: let the PC sleep if you like, but enable wake timers (or set
   it never to sleep); the task wakes it.

The task runs while you're signed in to Windows (WSL belongs to your user).
If the PC restarts for updates and sits at the sign-in screen, runs wait until
you sign in, then catch up.

## Secrets

Secrets live only in `.env` on the machine that runs the crawler:

- `.env` is listed in `.gitignore`, so it never reaches GitHub. Neither does
  `data/` (the database, raw archive, and local backups).
- `chmod 600 .env` so only your user can read it.
- The R2 backups contain the database and raw responses, never `.env`.
- Keep a copy of the `.env` values in a password manager: if the machine
  dies, `concertdb restore` brings back the data but not the secrets.
- If a key leaks, regenerate it at the provider and update `.env`.

## R2 bucket

The backup bucket should be **private**. Turn off the public `r2.dev`
development URL on it: with it on, anyone who finds the URL can download the
database snapshots. The crawler writes with an API token (Cloudflare dashboard →
R2 → Manage API tokens → "Object Read & Write" limited to this bucket), which
gives the four `R2_*` values in `.env`. If MusicSceneMap later needs a public
file (e.g. the exported `opened_for` edges), publish it to the existing public
graph bucket instead.
