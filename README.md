# FileMaker Viewer

A read-only Windows application for opening FileMaker `.fp3`, `.fp5`, `.fp7`,
and `.fmp12` files, browsing tables, and viewing records page by page.

## Getting started

1. Create the virtual environment with `python -m venv .venv`.
2. Run `Run.bat`.
3. Click **Browse…**, select a supported file, and click **Open**.

Click a column heading to sort records. Search starts after you enter at least
3 characters. When a database contains more than 10 tables, the app uses a
table selector. The app keeps a local cache so it does not need to reread the
source file each time. The **Structure** window lists decoded table and field
metadata, flags field-name relationship candidates such as `id_convegni`, and
shows a proposed normalized model for the Convegni/Aziende/Persone findings.
Use **Export JSON report…** to save the metadata and analysis without record
values. Prior observations from the live FileMaker UI are labeled separately
from facts decoded from the selected file.

## Supported formats and limitations

- Supports the four extensions listed in the `fmptools/HACKING` format guide:
  `.fp3`, `.fp5`, `.fp7`, and `.fmp12`.
- `.fp3` and `.fp5` use the legacy binary family; `.fp7` and `.fmp12` use the
  modern family. The reader checks the file header before parsing it.
- Files protected with **Encryption at Rest** cannot be read by this extractor.
- The reader extracts tables, field names, and tabular values. It also reads
  field types from the documented metadata in `.fp3` and `.fp5`; the guide does
  not describe enough metadata to recover field types in `.fp7` and `.fmp12`.
- The guide does not document FileMaker's relationship graph. The structure
  report suggests links from field names and possible junction tables; these
  are unverified and do not replace the relationship graph in FileMaker. Names
  such as `id_convegni` are reported only when actually found in decoded
  metadata; the proposed model's key names are conventions, not claims about
  the source file.
- Layouts, scripts, calculations, account privileges, and external container
  contents are not reconstructed. Reading the raw file does not enforce
  FileMaker account privileges.
- JSONL cache files are stored in the project `cache` folder. Logs and the list
  of recent files are stored under `%LOCALAPPDATA%`. These local files are
  excluded from the Git repository.

## Accelerated build

The app can use the included Python reader. To compile the reader with Cython,
install Cython and Microsoft C++ Build Tools, create `.venv`, and run
`BuildCython.bat`.

## Technical source and attribution

The bytecode, sector, and data-path descriptions come from
[Evan Miller's `fmptools/HACKING` guide](https://github.com/evanmiller/fmptools/blob/main/HACKING).
The format families and reference-reader behavior were also compared with the
[`evanmiller/fmptools` source repository](https://github.com/evanmiller/fmptools).
This project's Python reader implements the documented structures and includes
an interpretation of legacy field metadata; it does not bundle the `fmptools`
C source code.

`fmptools` is distributed under the MIT License. The attribution and license
text are included in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The
FileMaker format is proprietary, and the cited reverse-engineered notes may not
cover every file variation.
