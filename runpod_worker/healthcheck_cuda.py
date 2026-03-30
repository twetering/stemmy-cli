#!/usr/bin/env python3
"""Exit 0 only when CUDA + cuDNN are usable (RunPod / NVIDIA runtime)."""
import sys


def main() -> int:
    try:
        import torch
    except ImportError as e:
        print("torch import failed:", e)
        return 1

    if not torch.cuda.is_available():
        print("cuda not available")
        return 1

    try:
        v = torch.backends.cudnn.version()
    except Exception as e:
        print("cudnn version failed:", e)
        return 1

    print("cuda_ok", torch.cuda.get_device_name(0), "cudnn", v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
