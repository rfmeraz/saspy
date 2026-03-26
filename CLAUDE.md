# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

SASPy is a Python interface to SAS, enabling mixed Python/SAS workflows by connecting a Python process to SAS 9.4+ or SAS Viya 3+ deployments (local or remote).
Core operations: convert between SAS datasets and Pandas DataFrames / Arrow tables, execute SAS code, retrieve ODS results.

## Build and install

```bash
pip install -e .
# or
python setup.py install
```

No required dependencies. Optional extras: `pip install -e ".[pandas,parquet,colorLOG]"`

## Testing

Tests use `unittest` (not pytest) and require a live SAS session. Tests live in `saspy/tests/`.

```bash
# run all tests
python -m unittest discover -s saspy/tests -p 'test_*.py'

# run a single test file
python -m unittest saspy.tests.test_sassession

# run a single test method
python -m unittest saspy.tests.test_sassession.TestSASsession.test_method_name
```

Tests create real SAS sessions in `setUpClass()` and submit actual SAS code. Some tests skip when optional dependencies (duckdb, pyarrow) are absent via `@unittest.skipIf`.

## Architecture

### Layered design with pluggable I/O

```
Public API (__init__.py)
    └── sasbase.py (SASsession, SASconfig, common logic)
            ├── sasiostdio.py   (STDIO: local/SSH via fork/exec/pipes)
            ├── sasiohttp.py    (HTTP: SAS Viya REST API)
            ├── sasioiom.py     (IOM: Java bridge, Windows/Linux/MVS)
            └── sasiocom.py     (COM: Windows only)
```

### Where code goes

- **sasbase.py**: SASsession and SASconfig classes. All access-method-independent logic lives here. If a feature just generates SAS code to submit, it belongs in sasbase.
- **sasioXXX.py**: Access-method-specific transport. Each module implements the same interface with common signatures/return types. When a feature needs different implementations per access method, add an entry point in sasbase that delegates to the io module.
- **sasdata.py**: SASdata class — a reference to a SAS dataset. Methods for df/arrow/parquet conversion, filtering, head/tail. Delegates data operations through the session's I/O module.
- **sasproccommons.py**: Shared code generation utilities for SAS procedures.
- **sasstat.py / sasets.py / sasqc.py / sasml.py / sasViyaML.py**: Analytics modules wrapping SAS procedures. Use `@procDecorator` from `sasdecorator.py` for method generation. To add procs, follow the directions in the respective module.

### Configuration

Config search order: `~/.config/saspy/sascfg_personal.py` > `saspy/sascfg_personal.py` (in-package) > `saspy/sascfg.py` (default template). Each config names one or more connection definitions specifying the access method and its options.

### Required method attributes

All new methods must support:
1. **teach_me_sas** / `nosub`: return generated SAS code without executing it
2. **batch**: return results as object/dict instead of displaying them
3. **results=**: HTML vs TEXT output, driven by notebook vs terminal context

## Contribution conventions

- Be Pythonic — avoid unnecessary SAS abstractions (e.g., no SASlibname object)
- Contributions must pass existing regression tests and add new tests for new code
- Commits require a DCO sign-off line: `Signed-off-by: Name <email>`
