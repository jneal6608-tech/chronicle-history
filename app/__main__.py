"""`python -m app` starts the studio engine and opens it in Chrome.

    python -m app              start + open Chrome
    python -m app --no-browser start only (used by the launcher, which opens Chrome itself)
"""
import shutil
import subprocess
import sys
import threading
import webbrowser

import uvicorn

from . import config


def open_in_chrome(url: str) -> None:
    """On ChromeOS, garcon-url-handler opens the URL in the Chromebook's own Chrome."""
    for handler in ("garcon-url-handler", "xdg-open"):
        if shutil.which(handler):
            try:
                subprocess.Popen([handler, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
    webbrowser.open(url)


def main() -> None:
    urls = config.studio_urls()
    print("\n  Chronicle Shorts engine is running.")
    print(f"  Open in Chrome:  {urls[0]}")
    for alt in urls[1:]:
        print(f"  (or)            {alt}")
    print("  Stop it with Ctrl+C, or the 'Stop Chronicle Shorts' app.\n", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: open_in_chrome(urls[0])).start()
    uvicorn.run("app.server:app", host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
