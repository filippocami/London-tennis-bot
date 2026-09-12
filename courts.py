#!/usr/bin/env python3
"""
Royal Parks court watcher -> Telegram.

Checks tennis and padel availability at The Courts in Hyde Park and
The Regent's Park (Royal Parks / OpenPlay "Flow" booking system).

    python courts.py            # check and send to Telegram
    python courts.py --dry-run  # print, send nothing
    python courts.py --debug    # also dump rendered page text to ./debug/
"""

from __future__ import annotations

import argparse
import asyncio
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

BASE_URL = (
    "https://sportsandleisureroyalparks.bookings.flow.onl"
    "/location/{location}/{activity}/{date}/by-time"
)

# Courts open 7 days ahead, released daily at 07:00. Looking further is wasted
# effort: those pages either show nothing or show slots you can't book yet.
MAX_DAYS_AHEAD = 7

# Mon=0 ... Sun=6. {5, 6} = weekends only. Use set(range(7)) for every day.
WANTED_WEEKDAYS = {5, 6}

# Only report slots you'd actually play (24h clock, start time).
# Courts run 07:00-21:00, so this default reports everything.
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
# --------------------------------------------------------------------------

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
RANGE_RE = re.compile(r"\b[01]?\d:[0-5]\d\s*[-\u2013]\s*[01]?\d:[0-5]\d\b")

# Phrases that mean "yes, bookable".
POSITIVE = ("available", "book now", "spaces")

# Checked FIRST. Anything matching here is not a free slot, even though some
# of these contain the word "available".
NEGATIVE = (
    "not available",
    "no availability",
    "unavailable",
    "fully booked",
    "fully",
    "sold out",
    "member",
    "log in",
    "login",
    "sign in",
    "cookie",
)

# Court / resource label, e.g. "Court 3", "Padel 1".
COURT_RE = re.compile(r"\b(?:court|padel|pitch|rink)\s*\d+\b", re.I)
PRICE_RE = re.compile(r"£\s*\d+(?:\.\d{2})?")

# How far back to look for the time a slot line belongs to.
LOOKBACK = 8


def is_slot_line(line: str) -> bool:
    low = line.lower()
    if any(neg in low for neg in NEGATIVE):
        return False
    return any(pos in low for pos in POSITIVE)


def slot_hour(time_str: str) -> int | None:
    m = TIME_RE.search(time_str)
    return int(m.group(1)) if m else None


def parse_slots(page_text: str) -> list[str]:
    """Pull free slots out of the rendered page text.

    The Flow page lists a time, then the courts under it, so a slot line's
    time sits on a nearby preceding line. Track the most recent time seen and
    only pair it if it's within LOOKBACK lines.
    """
    lines = [l.strip() for l in page_text.split("\n") if l.strip()]
    slots: list[str] = []
    last_time: str | None = None
    last_time_idx = -999
    last_court: str | None = None
    last_court_idx = -999

    for i, line in enumerate(lines):
        tm = RANGE_RE.search(line) or TIME_RE.search(line)
        if tm and not any(neg in line.lower() for neg in NEGATIVE):
            last_time, last_time_idx = tm.group(0), i

        cm = COURT_RE.search(line)
        if cm:
            last_court, last_court_idx = cm.group(0), i

        if not is_slot_line(line):
            continue

        time_str = last_time if (i - last_time_idx) <= LOOKBACK else None
        if time_str:
            hour = slot_hour(time_str)
            if hour is not None and not (EARLIEST_HOUR <= hour <= LATEST_HOUR):
                continue

        court = last_court if (i - last_court_idx) <= 3 else None

        # Price usually renders just after the availability label.
        price = None
        for nxt in lines[i + 1 : i + 4]:
            pm = PRICE_RE.search(nxt)
            if pm:
                price = pm.group(0).replace(" ", "")
                break

        parts = [p for p in (time_str, court, price) if p]
        entry = "  ".join(parts) if parts else line

        if entry not in slots:
            slots.append(entry)

    return slots


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


async def check_url(page, url: str, debug: bool) -> list[str]:
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
            print(f"    {len(text.splitlines())} lines, {len(slots)} slot(s)")
            return slots

        except Exception as e:
            print(f"    attempt {attempt}/{RETRIES} failed: {e}", file=sys.stderr)
            if attempt < RETRIES:
                await page.wait_for_timeout(3_000)

    return []


async def collect(debug: bool) -> list[str]:
    dates = target_dates()
    if not dates:
        print("No target dates inside the booking window.")
        return []

    print(f"Checking dates: {', '.join(dates)}")
    blocks: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1280, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        )
        for date_str in dates:
            for loc_name, loc_slug in LOCATIONS.items():
                for activity in ACTIVITIES:
                    url = BASE_URL.format(
                        location=loc_slug, activity=activity, date=date_str
                    )
                    print(f"  {loc_name} / {activity} / {date_str}")
                    slots = await check_url(page, url, debug)
                    if slots:
                        day = datetime.strptime(date_str, "%Y-%m-%d").strftime(
                            "%A %d %b"
                        )
                        body = "\n".join(slots)
                        blocks.append(
                            f"{loc_name} | {activity.upper()} | {day}\n{body}\n{url}"
                        )
        await browser.close()

    return blocks


# --------------------------------------------------------------------------
# TELEGRAM
# --------------------------------------------------------------------------


def chunk(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on blank lines so a slot block is never cut in half."""
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
    blocks = await collect(args.debug)

    if blocks:
        msg = "Courts available\n\n" + "\n\n".join(blocks)
    else:
        msg = "No courts available in the booking window."

    print("\n" + "-" * 50 + f"\n{msg}\n" + "-" * 50)

    if args.dry_run:
        return
    if not blocks and not SEND_WHEN_EMPTY:
        print("Nothing found; staying quiet.")
        return
    send(msg)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print, don't send")
    ap.add_argument("--debug", action="store_true", help="dump page text to ./debug/")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
