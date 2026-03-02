"""Version consistency checks."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from rbxbundle import __version__


class TestVersionConsistency(unittest.TestCase):
    def test_init_and_pyproject_versions_match(self):
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")

        dynamic_attr = re.search(r'^version\s*=\s*\{\s*attr\s*=\s*"([^"]+)"\s*\}\s*$', content, re.MULTILINE)
        self.assertIsNotNone(dynamic_attr, "pyproject.toml must declare setuptools dynamic version attr")
        self.assertEqual(dynamic_attr.group(1), "rbxbundle.__version__")

        project_dynamic = re.search(r'^dynamic\s*=\s*\[(.*?)\]\s*$', content, re.MULTILINE)
        self.assertIsNotNone(project_dynamic, "[project] must mark version as dynamic")
        self.assertIn('"version"', project_dynamic.group(1))

        semver = re.compile(r"^\d+\.\d+\.\d+$")
        self.assertRegex(__version__, semver)


if __name__ == "__main__":
    unittest.main()
