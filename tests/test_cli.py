from __future__ import annotations

import unittest

from rbxbundle import _cli


class TestCliConfigDefaults(unittest.TestCase):
    def setUp(self) -> None:
        self.old_input = _cli.DEFAULT_INPUT_DIR
        self.old_output = _cli.DEFAULT_OUTPUT_DIR

    def tearDown(self) -> None:
        _cli.DEFAULT_INPUT_DIR = self.old_input
        _cli.DEFAULT_OUTPUT_DIR = self.old_output

    def test_apply_config_defaults_updates_argparse_defaults(self):
        cfg = {
            "input_dir": "custom-input",
            "output_dir": "custom-output",
        }

        _cli._apply_config_defaults(cfg)
        parser = _cli._build_argparser()
        subparsers = next(action for action in parser._actions if action.dest == "command").choices

        build_parser = subparsers["build"]
        list_parser = subparsers["list"]

        build_output = next(action for action in build_parser._actions if action.dest == "output")
        list_dir = next(action for action in list_parser._actions if action.dest == "dir")

        self.assertEqual(build_output.default, "custom-output")
        self.assertEqual(list_dir.default, "custom-input")

    def test_default_workspace_root_ends_with_rbxbundle(self):
        root = _cli._resolve_default_workspace_root()
        self.assertEqual(root.name.lower(), "rbxbundle")


class TestCliTextHelpers(unittest.TestCase):
    def test_helpers_use_ascii_markers(self):
        self.assertEqual(_cli.clr("", "[OK]"), "[OK]")
        self.assertEqual(_cli.clr("", "->"), "->")


class TestCliModeRouting(unittest.TestCase):
    def test_no_args_stays_interactive(self):
        self.assertFalse(_cli._should_use_argparse([]))

    def test_explicit_subcommand_uses_argparse(self):
        self.assertTrue(_cli._should_use_argparse(["build"]))

    def test_help_flag_uses_argparse(self):
        self.assertTrue(_cli._should_use_argparse(["--help"]))

    def test_unknown_argument_still_uses_argparse(self):
        self.assertTrue(_cli._should_use_argparse(["lisits"]))
