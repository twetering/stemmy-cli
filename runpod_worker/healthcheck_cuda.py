#!/usr/bin/env python3
"""Lightweight runtime check for faster-whisper CUDA dependencies."""
import sys


def main() -> int:
    try:
        import ctranslate2
    except ImportError as e:
        print("ctranslate2 import failed:", e)
        return 1

    try:
        supported = ctranslate2.get_supported_compute_types("cuda")
    except Exception as e:
        print("cuda probe failed:", e)
        return 1

    print("cuda_supported_compute_types", sorted(list(supported)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
