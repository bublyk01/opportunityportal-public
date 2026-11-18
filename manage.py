#!/usr/bin/env python
"""Django command-line entry point for OpportunityHub."""
import os
import sys


def main():
    # Windows pipes can otherwise use a legacy code page and crash while
    # reporting Ukrainian opportunity titles. Console and redirected output use
    # the same encoding; alternate test streams need no reconfiguration.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django is unavailable. Use .venv\\Scripts\\python.exe "
            "or install requirements.txt in your virtual environment."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
