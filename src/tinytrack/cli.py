"""CLI for tinytrack."""

import os
import sys

import uvicorn


def main():
    """Run the tinytrack server."""
    import argparse

    parser = argparse.ArgumentParser(
        description="tinytrack - Minimal, privacy-focused web analytics"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("TINYTRACK_HOST", "0.0.0.0"),
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TINYTRACK_PORT", "8000")),
        help="Port to listen on (default: 8000)",
    )
    args = parser.parse_args()

    print(f"Starting tinytrack on http://{args.host}:{args.port}")
    uvicorn.run("tinytrack.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
