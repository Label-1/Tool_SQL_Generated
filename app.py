"""Visual SQL Studio Windows application entry point."""
from __future__ import annotations
import sys
import traceback
from pathlib import Path


def main():
    try:
        from vsql.ui import launch
        launch()
    except Exception:
        root = Path(__file__).resolve().parent
        logs = root / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "startup_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        try:
            from tkinter import Tk, messagebox
            r = Tk()
            r.withdraw()
            messagebox.showerror("Visual SQL Studio", "Unable to start. See logs/startup_error.log")
            r.destroy()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
