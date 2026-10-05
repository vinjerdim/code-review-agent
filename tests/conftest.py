import pytest

from reviewer.github_io import PRData, PRFile

SAMPLE_PATCH = """\
@@ -1,4 +1,5 @@
 import os
-import sys
+import json
+import re

 def main():
@@ -20,3 +21,4 @@ def helper(x):
     y = x + 1
-    return y
+    z = y * 2
+    return z
"""


def make_file(filename="src/app.py", patch=SAMPLE_PATCH, status="modified", **kw) -> PRFile:
    return PRFile(filename=filename, status=status, patch=patch, **kw)


def make_pr(files=None, **kw) -> PRData:
    defaults = dict(
        repo="octo/demo",
        number=7,
        title="Add helper",
        body="Adds a helper.",
        author="dev",
        base_sha="base123",
        head_sha="head456",
    )
    defaults.update(kw)
    return PRData(files=files if files is not None else [make_file()], **defaults)


@pytest.fixture
def pr() -> PRData:
    return make_pr()
