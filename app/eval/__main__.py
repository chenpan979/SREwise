"""`python -m app.eval` 入口。"""

from .cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
