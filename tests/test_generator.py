"""Tests for rbxbundle.generator error handling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rbxbundle.generator import create_bundle


class TestCreateBundleErrors(unittest.TestCase):
    def test_malformed_xml_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            in_path = base / "broken.rbxmx"
            out_dir = base / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            in_path.write_text("<roblox><Item></roblox>", encoding="utf-8")

            with self.assertRaises(RuntimeError) as ctx:
                create_bundle(in_path, output_dir=out_dir, include_context=False)

            self.assertIn("XML parse error", str(ctx.exception))

    def test_dependency_failures_write_detailed_error_file(self):
        xml = """\
<roblox>
  <Item class="DataModel">
    <Item class="ModuleScript">
      <Properties>
        <string name="Name">Main</string>
        <ProtectedString name="Source">return {}</ProtectedString>
      </Properties>
    </Item>
  </Item>
</roblox>
"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            in_path = base / "sample.rbxmx"
            out_dir = base / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            in_path.write_text(xml, encoding="utf-8")

            with mock.patch("rbxbundle.generator.build_dependency_graph", side_effect=AttributeError("missing node map")):
                with self.assertRaises(RuntimeError) as ctx:
                    create_bundle(in_path, output_dir=out_dir, include_context=False)

            self.assertIn("Dependency extraction failed", str(ctx.exception))

            err_file = out_dir / "sample_bundle" / "DEPENDENCIES_ERROR.txt"
            self.assertTrue(err_file.exists())
            content = err_file.read_text(encoding="utf-8")
            self.assertIn("File: sample.rbxmx", content)
            self.assertIn("Section: dependencies", content)
            self.assertIn("Data type: graph", content)
            self.assertIn("Error type: AttributeError", content)
            self.assertIn("Error: missing node map", content)


if __name__ == "__main__":
    unittest.main()
