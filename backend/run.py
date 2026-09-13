"""Single-process backend launcher.

The API runs on its own by default; the Vite frontend is started separately
(see ``start-frontend``). Set ``frontend_serving_enabled: true`` to also serve
the bundled dashboard from this process.
"""

import threading
import webbrowser
from pathlib import Path

import uvicorn

from config import get_settings


def open_dashboard(url: str, delay_seconds: float) -> None:
    """Open the dashboard after Uvicorn has had time to bind its socket."""
    timer = threading.Timer(delay_seconds, webbrowser.open, args=(url,))
    timer.daemon = True
    timer.start()


def main() -> None:
    settings = get_settings()
    if settings.frontend_serving_enabled and settings.open_browser_on_start:
        dashboard_url = (
            f"http://{settings.app_host}:{settings.app_port}"
            f"{settings.frontend_mount_path}/"
        )
        open_dashboard(dashboard_url, settings.browser_open_delay_seconds)

    uvicorn.run(
        "main:app",
        app_dir=str(Path(__file__).resolve().parent),
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
