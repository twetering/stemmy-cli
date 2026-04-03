#!/usr/bin/env python3
"""
Stemmy Woordenboek — lokale HTTP-server

Start de server en open de browser automatisch.
Gebruik:
  python3 dict/server.py            # standaard poort 7842
  python3 dict/server.py 8080       # eigen poort
  python3 dict/server.py --letter G # open direct op letter G
"""
import argparse
import http.server
import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent.parent
SERVE_DIR = ROOT / "output" / "dictionary"
DEFAULT_PORT = 7842


def main():
    parser = argparse.ArgumentParser(description="Stemmy Woordenboek server")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT,
                        help=f"Poort (default: {DEFAULT_PORT})")
    parser.add_argument("--letter", default="P",
                        help="Letter om direct te openen (default: P)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Browser niet automatisch openen")
    args = parser.parse_args()

    if not SERVE_DIR.exists():
        print(f"❌ Output map niet gevonden: {SERVE_DIR}")
        print("   Genereer eerst een woordenboek:")
        print("   ./scripts/run_dictionary.sh P")
        sys.exit(1)

    # Zoek beschikbare HTML bestanden
    html_files = sorted(SERVE_DIR.glob("woordenboek_*.html"))
    if not html_files:
        print(f"❌ Geen woordenboek HTML gevonden in {SERVE_DIR}")
        print("   Genereer eerst een woordenboek:")
        print("   ./scripts/run_dictionary.sh P")
        sys.exit(1)

    letter = args.letter.upper()
    target_file = SERVE_DIR / f"woordenboek_{letter}.html"
    if not target_file.exists():
        # Pak de eerste beschikbare
        target_file = html_files[0]
        letter = target_file.stem.split("_")[-1]

    url = f"http://localhost:{args.port}/woordenboek_{letter}.html"

    print("📖 Stemmy Woordenboek")
    print(f"   Serveer: {SERVE_DIR}")
    print(f"   Beschikbaar: {', '.join(f.stem for f in html_files)}")
    print()
    print(f"   🌐 {url}")
    print()
    print("   Ctrl+C om te stoppen")
    print()

    os.chdir(SERVE_DIR)

    handler = http.server.SimpleHTTPRequestHandler
    handler.log_message = lambda *args: None  # stil

    httpd = http.server.HTTPServer(("", args.port), handler)

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n   Server gestopt.")


if __name__ == "__main__":
    main()
