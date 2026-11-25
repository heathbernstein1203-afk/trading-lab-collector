"""Most files in tests/ are plain scripts (run: python tests/test_x.py) with module-level code
and env-var state paths read at import time. Importing them all into one pytest process makes
them share modules and paths, so pytest skips importing them here and test_scripts.py runs each
one in its own fresh Python process instead."""
collect_ignore = ["test_bot.py", "test_demo_sign.py", "test_extras.py", "test_parse.py"]
