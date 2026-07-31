import io
import runpy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from ci.select_languages import format_outputs, main, select_languages


class SelectLanguagesTests(unittest.TestCase):
    def test_selects_only_cpp_for_cpp_change(self) -> None:
        self.assertEqual(select_languages(["cpp/main.cpp"]), ("cpp",))

    def test_selects_each_implementation_from_its_directory(self) -> None:
        self.assertEqual(select_languages(["rust/src/main.rs"]), ("rust",))
        self.assertEqual(select_languages(["go/main.go"]), ("go",))
        self.assertEqual(select_languages(["zig/src/main.zig"]), ("zig",))

    def test_selects_all_for_shared_conformance_change(self) -> None:
        self.assertEqual(
            select_languages(["shared/conformance.py"]),
            ("rust", "cpp", "go", "zig"),
        )
        self.assertEqual(
            select_languages(["SPEC.md"]),
            ("rust", "cpp", "go", "zig"),
        )

    def test_selects_all_for_ci_selection_change(self) -> None:
        self.assertEqual(
            select_languages([".gitea/workflows/ci.yml"]),
            ("rust", "cpp", "go", "zig"),
        )
        self.assertEqual(
            select_languages(["ci/select_languages.py"]),
            ("rust", "cpp", "go", "zig"),
        )

    def test_selects_all_when_forced_by_manual_dispatch(self) -> None:
        self.assertEqual(
            select_languages([], force_all=True),
            ("rust", "cpp", "go", "zig"),
        )

    def test_selects_all_for_unrecognized_root_change(self) -> None:
        self.assertEqual(
            select_languages(["install.sh"]),
            ("rust", "cpp", "go", "zig"),
        )
        self.assertEqual(
            select_languages(["mcp/server.py"]),
            ("rust", "cpp", "go", "zig"),
        )
        self.assertEqual(
            select_languages(["README.md"]),
            ("rust", "cpp", "go", "zig"),
        )
        self.assertEqual(
            select_languages(["cpp/main.cpp", "README.md"]),
            ("rust", "cpp", "go", "zig"),
        )

    def test_selects_nothing_for_empty_change_set(self) -> None:
        self.assertEqual(select_languages([""]), ())

    def test_formats_stable_github_outputs(self) -> None:
        self.assertEqual(
            format_outputs(("cpp", "go")),
            "rust=false\ncpp=true\ngo=true\nzig=false\nany=true\n",
        )
        self.assertEqual(
            format_outputs(()),
            "rust=false\ncpp=false\ngo=false\nzig=false\nany=false\n",
        )

    def test_main_reads_paths_from_standard_input(self) -> None:
        output = io.StringIO()
        with (
            patch.object(sys, "stdin", io.StringIO("cpp/main.cpp\ngo/main.go\n")),
            patch.object(sys, "stdout", output),
        ):
            self.assertEqual(main([]), 0)

        self.assertEqual(
            output.getvalue(),
            "rust=false\ncpp=true\ngo=true\nzig=false\nany=true\n",
        )

    def test_script_entry_point_selects_all(self) -> None:
        output = io.StringIO()
        script_path = Path(__file__).with_name("select_languages.py")
        with (
            patch.object(sys, "argv", [str(script_path), "--all"]),
            patch.object(sys, "stdin", io.StringIO()),
            patch.object(sys, "stdout", output),
            self.assertRaisesRegex(SystemExit, "^0$"),
        ):
            runpy.run_path(str(script_path), run_name="__main__")

        self.assertEqual(
            output.getvalue(),
            "rust=true\ncpp=true\ngo=true\nzig=true\nany=true\n",
        )


if __name__ == "__main__":
    unittest.main()
