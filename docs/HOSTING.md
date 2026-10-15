# Where to run it

## What the job needs

- **Compute:** almost nothing. One run a day takes ~15–30 minutes, mostly
  waiting between requests (setlist.fm: 1,400 requests at ~0.6 s apart).
- **Memory:** well under 1 GB.
- **Disk:** a few GB over the first year (database + compressed raw archive).
- **Uptime:** it has to run *once a day*, not continuously.

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

**A Raspberry Pi 5 with a small USB SSD**, or any always-on computer you
already have. The residential IP matters more than reliability here: the
capture step reads venue and ticketing pages, and those are the sites most
likely to block cloud servers. R2 backups make the hardware disposable.

Skip EC2 unless you specifically want it on AWS; a ~$5 VPS does the same job
for less. If you'd rather not own hardware, start with a VPS and move to a Pi
later; `concertdb restore` makes the move a five-minute job.

## Pi setup sketch

1. Raspberry Pi OS Lite (64-bit), boot from (or mount) the SSD.
2. `sudo apt install python3-venv git`, then follow Setup in the README.
3. Put `.env` in the repo directory (`chmod 600 .env`).
4. Install the timer (instructions at the top of `deploy/concertdb.service`).
5. `systemctl --user list-timers` to confirm; `journalctl --user -u concertdb` for logs.
