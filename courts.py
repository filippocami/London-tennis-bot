#!/usr/bin/env python3
"""
Royal Parks court watcher -> Telegram.

Tennis and padel availability at The Courts in Hyde Park and The Regent's Park
(Royal Parks / OpenPlay "Flow" booking system).

    python courts.py            # check and send to Telegram
    python courts.py --dry-run  # print, send nothing
    python courts.py --debug    # also dump rendered page text to ./debug/
"""

from __future__ import annotations

import argparse
import asyncio
import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

LOCATIONS = {
    "Hyde Park": "hyde-park-courts",
    "Regent's Park": "the-regents-park-courts",
}

ACTIVITIES = ["tennis", "padel"]

ACTIVITY_LABELS = {"tennis": "TENNIS", "padel": "PADEL"}

BASE_URL = (
    "https://sportsandleisureroyalparks.bookings.flow.onl"
    "/location/{location}/{activity}/{date}/by-time"
)

# Courts open 7 days ahead, released daily at 07:00. Don't raise this.
MAX_DAYS_AHEAD = 7

# Mon=0 ... Sun=6. {5, 6} = weekends only. Use set(range(7)) for every day.
WANTED_WEEKDAYS = {5, 6}

# Only report slots starting in this window (24h clock).
EARLIEST_HOUR = 7
LATEST_HOUR = 21

# Send a message even when nothing is free? False keeps the chat quiet.
SEND_WHEN_EMPTY = False

PAGE_TIMEOUT_MS = 45_000
SETTLE_MS = 6_000
RETRIES = 2
TELEGRAM_LIMIT = 4096

# --------------------------------------------------------------------------
# PARSING
#
# The booking page lists one row per time slot, and the rendered text of a row
# looks like:
#
#     07:00 - 08:00
#     60min
#     Tennis-60min
#     Multiple                 <- or "Padel Court 1"
#     4 spaces available       <- or "Fully booked"
#     Book
#
# So we anchor on the time range, then read the status from inside that row
# only. Searching the whole page for the word "available" does NOT work: the
# page has an "Available only" filter chip that matches on every single page.
# --------------------------------------------------------------------------

# "07:00 - 08:00". Deliberately strict: won't match "06:00am - 10:00pm"
# (the day summary header) or the "06.00 / 22.00" slider labels.
ROW_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)\s*[-\u2013]\s*([01]?\d|2[0-3]):([0-5]\d)$")

SPACES_RE = re.compile(r"(\d+)\s+spaces?\s+available", re.I)
COURT_RE = re.compile(r"\b(?:padel\s+)?(?:court|pitch|rink)\s*\d+\b", re.I)

# A row is bookable if its status line says one of these.
FREE_EXACT = ("available", "book now")

# ...and definitely not if it says one of these.
TAKEN = (
    "fully booked",
    "not available",
    "no availability",
    "unavailable",
    "sold out",
    "members only",
    "closed",
)

# How many lines after the time range still count as part of the row.
ROW_WINDOW = 10


def parse_slots(page_text: str) -> list[dict]:
    """Return [{'time': '07:00 - 08:00', 'spaces': 4, 'court': 'Court 1'}, ...]"""
    lines = [l.strip() for l in page_text.split("\n") if l.strip()]

    # Index every line that starts a slot row.
    starts = [i for i, l in enumerate(lines) if ROW_RE.match(l)]
    slots: list[dict] = []

    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        end = min(end, start + ROW_WINDOW)
        row = lines[start:end]

        time_str = lines[start]
        hour = int(ROW_RE.match(time_str).group(1))
        if not (EARLIEST_HOUR <= hour <= LATEST_HOUR):
            continue

        spaces = None
        free = False
        for line in row[1:]:
            low = line.lower()
            if any(t in low for t in TAKEN):
                free = False
                break
            m = SPACES_RE.search(line)
            if m:
                spaces = int(m.group(1))
                free = spaces > 0
                break
            if low in FREE_EXACT:
                free = True
                break

        if not free:
            continue

        court = None
        for line in row[1:]:
            m = COURT_RE.search(line)
            if m:
                court = m.group(0)
                break

        slots.append({"time": time_str, "spaces": spaces, "court": court})

    # Same time can appear twice (two padel courts); keep the richest entry.
    best: dict[str, dict] = {}
    for s in slots:
        key = f"{s['time']}|{s['court'] or ''}"
        best.setdefault(key, s)
    return sorted(best.values(), key=lambda s: s["time"])


# --------------------------------------------------------------------------
# FETCHING
# --------------------------------------------------------------------------


def target_dates() -> list[str]:
    today = datetime.now(timezone.utc)
    out = []
    for i in range(MAX_DAYS_AHEAD + 1):
        day = today + timedelta(days=i)
        if day.weekday() in WANTED_WEEKDAYS:
            out.append(day.strftime("%Y-%m-%d"))
    return out


async def check_url(page, url: str, debug: bool) -> list[dict]:
    for attempt in range(1, RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass  # networkidle is flaky on pages that keep polling
            await page.wait_for_timeout(SETTLE_MS)

            text = await page.inner_text("body")

            if debug:
                Path("debug").mkdir(exist_ok=True)
                name = re.sub(r"[^a-zA-Z0-9]+", "_", url)[-100:]
                Path(f"debug/{name}.txt").write_text(text)

            if len(text.strip()) < 200:
                raise RuntimeError("page looks empty (still loading?)")

            slots = parse_slots(text)
            rows = sum(1 for l in text.split("\n") if ROW_RE.match(l.strip()))
            print(f"    {rows} time rows, {len(slots)} free")
            return slots

        except Exception as e:
            print(f"    attempt {attempt}/{RETRIES} failed: {e}", file=sys.stderr)
            if attempt < RETRIES:
                await page.wait_for_timeout(3_000)

    return []


async def collect(debug: bool) -> dict:
    """-> {activity: {venue: {date: [slots]}}}"""
    dates = target_dates()
    if not dates:
        print("No target dates inside the booking window.")
        return {}

    print(f"Checking dates: {', '.join(dates)}")
    found: dict = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1280, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        )
        for activity in ACTIVITIES:
            for venue, slug in LOCATIONS.items():
                for date_str in dates:
                    url = BASE_URL.format(
                        location=slug, activity=activity, date=date_str
                    )
                    print(f"  {activity} / {venue} / {date_str}")
                    slots = await check_url(page, url, debug)
                    if slots:
                        found.setdefault(activity, {}).setdefault(venue, {})[
                            date_str
                        ] = {"slots": slots, "url": url}
        await browser.close()

    return found


# --------------------------------------------------------------------------
# MESSAGE
# --------------------------------------------------------------------------


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def format_message(found: dict) -> str:
    """Grouped by sport, then venue, then date. The date is a clickable link
    so the message stays short instead of carrying raw URLs."""
    out: list[str] = []

    for activity in ACTIVITIES:
        by_venue = found.get(activity)
        if not by_venue:
            continue

        total = sum(
            len(d["slots"]) for venue in by_venue.values() for d in venue.values()
        )
        label = ACTIVITY_LABELS.get(activity, activity.upper())
        out.append(f"<b>{label}</b>  ({total} free)")

        for venue in LOCATIONS:
            by_date = by_venue.get(venue)
            if not by_date:
                continue
            out.append(f"\n<b>{esc(venue)}</b>")

            for date_str in sorted(by_date):
                entry = by_date[date_str]
                day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b")
                bits = []
                for s in entry["slots"]:
                    start = s["time"].split("-")[0].strip()
                    extra = []
                    if s["court"]:
                        extra.append(s["court"])
                    if s["spaces"]:
                        extra.append(f"{s['spaces']} left")
                    bits.append(
                        f"{start} ({', '.join(extra)})" if extra else start
                    )
                times = ", ".join(bits)
                link = entry["url"]
                out.append(f'<a href="{link}">{day}</a>  {esc(times)}')

        out.append("")  # blank line between sports

    return "\n".join(out).strip()


def chunk(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            if current:
                parts.append(current)
            while len(block) > limit:
                parts.append(block[:limit])
                block = block[limit:]
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def send(text: str) -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        sys.exit("Set TELEGRAM_TOKEN and CHAT_ID.")

    for i, part in enumerate(chunk(text), 1):
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
            sys.exit(1)
        print(f"Sent part {i}.")


# --------------------------------------------------------------------------


async def run(args) -> None:
    found = await collect(args.debug)
    msg = format_message(found) if found else "No courts free in the booking window."

    print("\n" + "-" * 55 + f"\n{msg}\n" + "-" * 55)

    if args.dry_run:
        return
    if not found and not SEND_WHEN_EMPTY:
        print("Nothing free; staying quiet.")
        return
    send(msg)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print, don't send")
    ap.add_argument("--debug", action="store_true", help="dump page text to ./debug/")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
