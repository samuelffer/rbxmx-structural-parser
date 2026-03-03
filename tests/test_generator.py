#Tests for rbxbundle.generator error handling.

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rbxbundle.generator import ScriptRecord, create_bundle, generate_summary


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
                bundle_dir, zip_path, scripts = create_bundle(in_path, output_dir=out_dir, include_context=False)

            self.assertTrue(bundle_dir.exists())
            self.assertTrue(zip_path.exists())
            self.assertEqual(len(scripts), 1)

            err_file = out_dir / "sample_bundle" / "DEPENDENCIES_ERROR.txt"
            self.assertTrue(err_file.exists())
            content = err_file.read_text(encoding="utf-8")
            self.assertIn("File: sample.rbxmx", content)
            self.assertIn("Section: dependencies", content)
            self.assertIn("Data type: graph", content)
            self.assertIn("Error type: AttributeError", content)
            self.assertIn("Error: missing node map", content)

            summary = (out_dir / "sample_bundle" / "SUMMARY.md").read_text(encoding="utf-8")
            self.assertIn("Dependencies could not be analyzed; see `DEPENDENCIES_ERROR.txt`.", summary)

class TestGenerateSummaryBoundaryAlerts(unittest.TestCase):
    def test_boundary_alert_for_localscript_to_server_path(self):
        scripts = [
            ScriptRecord(
                class_name="LocalScript",
                name="ClientMain",
                full_path="StarterPlayer/StarterPlayerScripts/ClientMain",
                rel_file="scripts/StarterPlayer/StarterPlayerScripts/ClientMain.client.lua",
                source_len=10,
            )
        ]
        edges_json = [
            {
                "from": "StarterPlayer/StarterPlayerScripts/ClientMain",
                "to": "ServerScriptService/ServerMain",
                "kind": "require",
                "confidence": 1.0,
                "loc": {"line": 12},
            }
        ]

        summary = generate_summary(
            source_file="sample.rbxmx",
            scripts=scripts,
            contexts=[],
            attributes=[],
            nodes_json=[],
            edges_json=edges_json,
            include_context=False,
        )

        self.assertIn("## Client/Server Boundary Alerts", summary)
        self.assertIn("LocalScript depending on server-only path", summary)
        self.assertIn("line 12", summary)

    def test_no_boundary_alert_for_module_script_dependency(self):
        scripts = [
            ScriptRecord(
                class_name="ModuleScript",
                name="Shared",
                full_path="ReplicatedStorage/Shared",
                rel_file="scripts/ReplicatedStorage/Shared.lua",
                source_len=10,
            )
        ]
        edges_json = [
            {
                "from": "ReplicatedStorage/Shared",
                "to": "ReplicatedStorage/Util",
                "kind": "require",
                "confidence": 1.0,
                "loc": {"line": 3},
            }
        ]

        summary = generate_summary(
            source_file="sample.rbxmx",
            scripts=scripts,
            contexts=[],
            attributes=[],
            nodes_json=[],
            edges_json=edges_json,
            include_context=False,
        )

        self.assertIn("## Client/Server Boundary Alerts", summary)
        self.assertIn("*(no client/server boundary alerts)*", summary)


class TestGenerateSummaryFormatting(unittest.TestCase):
    def test_summary_uses_clean_output_markers(self):
        summary = generate_summary(
            source_file="sample.rbxmx",
            scripts=[],
            contexts=[],
            attributes=[],
            nodes_json=[],
            edges_json=[
                {
                    "from": "StarterPlayer/StarterPlayerScripts/ClientMain",
                    "to": None,
                    "kind": "dynamic",
                    "expr": 'require(configFolder:WaitForChild(configName))',
                    "confidence": 0.0,
                    "loc": {"line": 8},
                }
            ],
            include_context=False,
        )

        self.assertIn("# RBXBundle - Project Summary", summary)
        self.assertIn("? `StarterPlayer/StarterPlayerScripts/ClientMain` -> *(unresolved)*", summary)
        self.assertNotIn("â", summary)


if __name__ == "__main__":
    unittest.main()
