from __future__ import annotations

import uvicorn

from smart_qr.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "smart_qr.api.app:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
