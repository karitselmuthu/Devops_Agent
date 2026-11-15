#!/usr/bin/env python3
"""Supply a GitHub token to git without putting it in a remote URL."""

import os
import sys


if "username" in " ".join(sys.argv[1:]).lower():
    print("x-access-token")
else:
    print(os.environ["GITHUB_TOKEN"])
