"""PyInstaller entry point. Keep the service identical to python -m assistant."""
from multiprocessing import freeze_support

from assistant.__main__ import main

if __name__ == "__main__":
    freeze_support()
    main()
