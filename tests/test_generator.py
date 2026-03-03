#Tests for rbxbundle.generator error handling.

from __future__ import annotations

import csv
import json
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

    def test_run_context_controls_exec_side_export_and_metadata(self):
        xml = """\
<roblox>
  <Item class="DataModel">
    <Item class="StarterPlayer">
      <Properties>
        <string name="Name">StarterPlayer</string>
      </Properties>
      <Item class="Script">
        <Properties>
          <string name="Name">ClientController</string>
          <token name="RunContext">2</token>
          <ProtectedString name="Source">return Players.LocalPlayer</ProtectedString>
        </Properties>
      </Item>
      <Item class="LocalScript">
        <Properties>
          <string name="Name">LegacyLocal</string>
          <ProtectedString name="Source">print("legacy client")</ProtectedString>
        </Properties>
      </Item>
      <Item class="Script">
        <Properties>
          <string name="Name">ServerController</string>
          <token name="RunContext">1</token>
          <ProtectedString name="Source">print("server")</ProtectedString>
        </Properties>
      </Item>
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

            bundle_dir, zip_path, scripts = create_bundle(in_path, output_dir=out_dir, include_context=False)

            self.assertTrue(bundle_dir.exists())
            self.assertTrue(zip_path.exists())
            self.assertEqual(len(scripts), 3)

            client_file = bundle_dir / "scripts" / "StarterPlayer" / "ClientController.client.lua"
            legacy_local_file = bundle_dir / "scripts" / "StarterPlayer" / "LegacyLocal.client.lua"
            server_file = bundle_dir / "scripts" / "StarterPlayer" / "ServerController.server.lua"

            self.assertTrue(client_file.exists())
            self.assertTrue(legacy_local_file.exists())
            self.assertTrue(server_file.exists())

            self.assertIn("-- RunContext: Client (2)", client_file.read_text(encoding="utf-8"))
            self.assertIn("-- ExecSide: client", client_file.read_text(encoding="utf-8"))
            self.assertIn("-- RunContext: Legacy (0)", legacy_local_file.read_text(encoding="utf-8"))
            self.assertIn("-- ExecSide: client", legacy_local_file.read_text(encoding="utf-8"))
            self.assertIn("-- RunContext: Server (1)", server_file.read_text(encoding="utf-8"))
            self.assertIn("-- ExecSide: server", server_file.read_text(encoding="utf-8"))

            with (bundle_dir / "INDEX.csv").open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(
                set(rows[0].keys()),
                {
                    "class",
                    "name",
                    "path",
                    "file",
                    "source_len",
                    "run_context_value",
                    "run_context_name",
                    "exec_side",
                    "disabled",
                },
            )

            rows_by_name = {row["name"]: row for row in rows}
            self.assertEqual(
                rows_by_name["ClientController"]["file"].replace("\\", "/"),
                "scripts/StarterPlayer/ClientController.client.lua",
            )
            self.assertEqual(rows_by_name["ClientController"]["run_context_value"], "2")
            self.assertEqual(rows_by_name["ClientController"]["run_context_name"], "Client")
            self.assertEqual(rows_by_name["ClientController"]["exec_side"], "client")
            self.assertEqual(rows_by_name["ClientController"]["disabled"], "false")
            self.assertEqual(rows_by_name["LegacyLocal"]["run_context_value"], "0")
            self.assertEqual(rows_by_name["LegacyLocal"]["run_context_name"], "Legacy")
            self.assertEqual(rows_by_name["LegacyLocal"]["exec_side"], "client")
            self.assertEqual(rows_by_name["LegacyLocal"]["disabled"], "false")
            self.assertEqual(rows_by_name["ServerController"]["run_context_value"], "1")
            self.assertEqual(rows_by_name["ServerController"]["exec_side"], "server")
            self.assertEqual(rows_by_name["ServerController"]["disabled"], "false")

            summary = (bundle_dir / "SUMMARY.md").read_text(encoding="utf-8")
            self.assertIn("**Client Scripts:** 2", summary)
            self.assertIn("**Server Scripts:** 1", summary)
            self.assertIn("**Modules:** 0", summary)
            self.assertIn("`StarterPlayer/ClientController` (client)", summary)
            self.assertIn("`StarterPlayer/LegacyLocal` (client)", summary)
            self.assertIn("`StarterPlayer/ServerController` (server)", summary)

    def test_bundle_adds_disabled_warnings_hierarchy_min_and_manifest(self):
        xml = """\
<roblox>
  <Item class="DataModel">
    <Item class="Workspace">
      <Properties>
        <string name="Name">Workspace</string>
      </Properties>
      <Item class="Part">
        <Properties>
          <string name="Name">Baseplate</string>
        </Properties>
      </Item>
    </Item>
    <Item class="ReplicatedStorage">
      <Properties>
        <string name="Name">ReplicatedStorage</string>
      </Properties>
      <Item class="Script">
        <Properties>
          <string name="Name">LegacyShared</string>
          <ProtectedString name="Source">print("legacy")</ProtectedString>
        </Properties>
      </Item>
      <Item class="Folder">
        <Properties>
          <string name="Name">Config</string>
        </Properties>
      </Item>
      <Item class="RemoteEvent">
        <Properties>
          <string name="Name">BootRemote</string>
        </Properties>
      </Item>
    </Item>
    <Item class="ServerScriptService">
      <Properties>
        <string name="Name">ServerScriptService</string>
      </Properties>
      <Item class="Script">
        <Properties>
          <string name="Name">ServerMain</string>
          <token name="RunContext">1</token>
          <ProtectedString name="Source">print("server")</ProtectedString>
        </Properties>
      </Item>
    </Item>
    <Item class="StarterPlayer">
      <Properties>
        <string name="Name">StarterPlayer</string>
      </Properties>
      <Item class="LocalScript">
        <Properties>
          <string name="Name">ClientMain</string>
          <ProtectedString name="Source">print("client")</ProtectedString>
        </Properties>
      </Item>
      <Item class="Script">
        <Properties>
          <string name="Name">DisabledClient</string>
          <token name="RunContext">2</token>
          <bool name="Disabled">true</bool>
          <ProtectedString name="Source">print(Players.LocalPlayer)</ProtectedString>
        </Properties>
      </Item>
    </Item>
  </Item>
</roblox>
"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            in_path = base / "bundle.rbxmx"
            out_dir = base / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            in_path.write_text(xml, encoding="utf-8")

            bundle_dir, zip_path, scripts = create_bundle(in_path, output_dir=out_dir, include_context=False)

            self.assertTrue(bundle_dir.exists())
            self.assertTrue(zip_path.exists())
            self.assertEqual(len(scripts), 4)
            self.assertTrue((bundle_dir / "WARNINGS.txt").exists())
            self.assertTrue((bundle_dir / "HIERARCHY_MIN.txt").exists())
            self.assertTrue((bundle_dir / "MANIFEST.json").exists())

            disabled_file = bundle_dir / "scripts" / "StarterPlayer" / "DisabledClient.client.lua"
            self.assertIn("-- Disabled: true", disabled_file.read_text(encoding="utf-8"))

            with (bundle_dir / "INDEX.csv").open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            rows_by_name = {row["name"]: row for row in rows}
            self.assertEqual(rows_by_name["DisabledClient"]["disabled"], "true")
            self.assertEqual(rows_by_name["DisabledClient"]["exec_side"], "client")

            warnings_text = (bundle_dir / "WARNINGS.txt").read_text(encoding="utf-8")
            self.assertIn("WARN | ReplicatedStorage/LegacyShared | Legacy RunContext script under ReplicatedStorage may not execute without a loader.", warnings_text)

            hierarchy_full = (bundle_dir / "HIERARCHY.txt").read_text(encoding="utf-8").splitlines()
            hierarchy_min = (bundle_dir / "HIERARCHY_MIN.txt").read_text(encoding="utf-8").splitlines()
            self.assertTrue(hierarchy_min)
            self.assertLess(len(hierarchy_min), len(hierarchy_full))
            self.assertIn("ReplicatedStorage/LegacyShared (Script)", hierarchy_min)
            self.assertIn("ReplicatedStorage/Config (Folder)", hierarchy_min)
            self.assertIn("ReplicatedStorage/BootRemote (RemoteEvent)", hierarchy_min)
            self.assertNotIn("Workspace/Baseplate (Part)", hierarchy_min)

            manifest = json.loads((bundle_dir / "MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["input_filename"], "bundle.rbxmx")
            self.assertIn("rbxbundle_version", manifest)
            self.assertIn("generated_at", manifest)
            self.assertIn("sha256", manifest)
            self.assertEqual(len(manifest["sha256"]), 64)
            self.assertEqual(
                manifest["counts"],
                {
                    "scripts_total": 4,
                    "modules_total": 0,
                    "client_total": 2,
                    "server_total": 2,
                    "edges_total": 0,
                    "nodes_total": 4,
                    "warnings_total": 1,
                },
            )

            summary = (bundle_dir / "SUMMARY.md").read_text(encoding="utf-8")
            self.assertIn("## Entry Points", summary)
            self.assertIn("### Server Entry Points", summary)
            self.assertIn("### Client Entry Points", summary)
            self.assertIn("## Disabled Scripts (1)", summary)
            self.assertIn("`StarterPlayer/DisabledClient` (Script, client)", summary)
            self.assertIn("`StarterPlayer/DisabledClient` (client) *(disabled)*", summary)
            entry_points_section = summary.split("## Entry Points", 1)[1].split("## Disabled Scripts (1)", 1)[0]
            self.assertIn("`ServerScriptService/ServerMain` (server)", entry_points_section)
            self.assertIn("`ReplicatedStorage/LegacyShared` (server)", entry_points_section)
            self.assertIn("`StarterPlayer/ClientMain` (client)", entry_points_section)
            self.assertNotIn("DisabledClient", entry_points_section)

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
        self.assertIn("client-side script depending on server-only path", summary)
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
