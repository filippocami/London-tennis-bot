name: Court watcher

on:
  schedule:
    # Every 30 minutes, 06:00-22:30 London during BST (UTC+1).
    # After the clocks change on 25 Oct, shift to "*/30 6-22 * * *".
    # Overnight runs are skipped: nobody cancels a court at 04:00.
    - cron: "*/30 5-21 * * *"
  workflow_dispatch:

# If a run overruns, don't let the next one start on top of it.
concurrency:
  group: court-watcher
  cancel-in-progress: false

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 15

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      # Remembers which slots have already been reported. The runner is wiped
      # after every run, so without this the bot has no memory and would
      # re-send the same list every 30 minutes.
      # The key is unique per run so it always writes a fresh cache; the
      # restore-key prefix pulls in the most recent one from the last run.
      - name: Restore seen-slots memory
        uses: actions/cache@v4
        with:
          path: state
          key: courts-state-${{ github.run_id }}
          restore-keys: |
            courts-state-

      - name: Install dependencies
        run: |
          pip install httpx playwright
          playwright install --with-deps chromium

      - name: Check courts
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          CHAT_ID: ${{ secrets.CHAT_ID }}
        run: python courts.py

      - name: Upload debug output
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: debug-pages
          path: debug/
          if-no-files-found: ignore
