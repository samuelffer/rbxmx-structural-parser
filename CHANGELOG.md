# Changelog

All notable changes to this project will be documented in this file.

## [0.5.4] - 2026-03-03

### Fixed
- Fixed command-line routing so invalid arguments return an argparse error instead of opening interactive mode.

### Changed
- Default workspace for installed command-line usage now lives in `Documents/rbxbundle/`.
- The standalone `.exe` continues to use the folder where the executable is located.
- Updated README guidance for interactive mode, command-line usage, and default workspace paths.

## [0.5.3] - 2026-03-03

### Fixed
- Fixed packaged `.exe` startup so opening it without arguments no longer exits immediately.

### Changed
- Simplified startup behavior: launching without arguments now always opens interactive mode.
- Removed the startup mode toggle from settings and CLI help text.

## [0.5.2] - 2026-03-03

### Fixed
- Fixed config persistence defaults in argparse mode
- Fixed corrupted CLI and summary output text
- Added regression tests for CLI defaults and summary formatting

## [0.5.1] - 2026-03-02

### Fixed
- Fixed CLI config persistence to use a per-user config path instead of writing beside package files.
- Added support for `rbxbundle --version`.
- Normalized CLI status and error messages for clearer output.
- Updated tests to match the current dependency analysis failure message in `SUMMARY.md`.

## [0.5.0] - 2026-03-02

### Changed
- Moved the CLI entry point into the package as `rbxbundle._cli`.

### Fixed
- Updated the console script entry point to `rbxbundle._cli:main`.
- Ignored runtime config files in Git tracking.
- Bumped the package version to `0.5.0`.

## [0.4.1] - 2026-03-02

### Added
- Added client/server boundary alerts in summary generation.
- Improved dependency analysis reporting while keeping bundle generation working on dependency errors.

### Fixed
- Updated versioning and related test expectations.
