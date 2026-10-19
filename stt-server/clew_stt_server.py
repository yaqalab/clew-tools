"""Entry point for the Windows build (build_exe.ps1): the alefa-stt command line."""

import multiprocessing
import sys

from alefa_stt.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
