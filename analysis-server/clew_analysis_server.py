"""The analysis engine as one program: what PyInstaller packs for the Windows tray (build_exe.ps1)."""

from clew_analysis.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
