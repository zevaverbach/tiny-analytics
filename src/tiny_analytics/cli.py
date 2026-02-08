"""CLI for tiny-analytics."""

import os
import sys

import uvicorn


def main():
    """Run the tiny-analytics server."""
    import argparse

    parser = argparse.ArgumentParser(
        description="tiny-analytics - Minimal, privacy-focused web analytics"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("TINY_ANALYTICS_HOST", "0.0.0.0"),
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TINY_ANALYTICS_PORT", "8000")),
        help="Port to listen on (default: 8000)",
    )
    args = parser.parse_args()

    print(f"Starting tiny-analytics on http://{args.host}:{args.port}")
    uvicorn.run("tiny_analytics.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
