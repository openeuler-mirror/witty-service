import argparse
import logging
import sys
from typing import Optional

from witty_service.main import create_app
from witty_service.workspace_init import init_workspace
from uvicorn import run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="witty-service",
        description="Witty Service - A service for managing agents and sessions",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind to",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind to",
    )

    parser.add_argument(
        "--log-level",
        type=str,
        default="info",
        choices=["debug", "info", "warning", "error", "critical"],
        help="Logging level",
    )

    parser.add_argument(
        "--reload",
        action="store_true",
        default=False,
        help="Enable auto-reload on code changes (development only)",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of worker processes",
    )

    args = parser.parse_args()

    logger.setLevel(args.log_level.upper())

    logger.info(f"Starting Witty Service on {args.host}:{args.port}")
    logger.info(f"Log level: {args.log_level.upper()}")

    try:
        init_workspace()
        run(
            create_app,
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=args.workers,
            factory=True,
            log_level=args.log_level,
        )
    except Exception as e:
        logger.error(f"Failed to start Witty Service: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()