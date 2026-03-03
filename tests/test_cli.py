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
            "startup_mode": "interactive",
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


class TestCliTextHelpers(unittest.TestCase):
    def test_helpers_use_ascii_markers(self):
        self.assertEqual(_cli.clr("", "[OK]"), "[OK]")
        self.assertEqual(_cli.clr("", "->"), "->")
