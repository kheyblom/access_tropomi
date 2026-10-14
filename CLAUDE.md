# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Download the TROPOMI L2 HDO (SRON H2O/HDO) netCDF files from the SRON dCache WebDAV
server, https://tropomi.grid.surfsara.nl/hdo/, onto GLADE for the shared data lake. Most
of the record is already on disk. The job is to fetch only what is missing, plus any
local file whose size differs from the remote. This is one of a family of sibling
downloaders in `../` (`access_imerg`, `access_mswep`, `access_gleam`, `access_smap`).
It must stay consistent with them in layout, config shape, logging and style, except
where a deviation is recorded below (see *Settled design choices*).

Task list and status live in [TASKS.md](TASKS.md). Mark tasks complete there as they
finish. Keep the *Current working state* section below up to date at the end of every
working step, so that a new session can resume without re-deriving anything.

## Current working state

- Task 1 (this CLAUDE.md) and task 2 (git repo, `origin` connected) are done
  (2026-10-14). The first commit (`afde1e0`) holds `.gitignore` (sibling copy plus
  `logs/*.pid`), `CLAUDE.md` and `TASKS.md`. It is pushed to `origin/main`.
- Next: task 3. Plan the build with the user first (the proposed design is below and in
  TASKS.md). Nothing is built yet.

## Settled design choices (with the user, 2026-10-14)

- **Local layout mirrors the remote** inside the existing tree:
  `<download>/tropomi.grid.surfsara.nl/hdo/YYYY/MM/DD/<orbit>/<file>.nc`, with
  `directories.download` = `/glade/derecho/scratch/kheyblom/data/wisosat/raw/TROPOMI/`.
  This is a deliberate deviation from the sibling `<download>/<version>/raw/<product>/`
  layout. It means the ~30,800 files already on disk are recognized in place and nothing
  is moved. Directories stay tiny (about 15 orbit directories per day, one file each).
- **Skip rule**: skip a file whose local size equals the remote size. A local file of a
  different size is re-downloaded to `<file>.part` and renamed over the old one only
  once it is complete.
- The run never deletes or moves files it did not write. It sweeps only its own `.part`
  files. Strays already in the tree (see below) are reported, and are cleaned up only
  with the user's approval.

Proposed in TASKS.md task 3 and not yet confirmed (plan task 3 with the user before
building):
- Enumerate with WebDAV `PROPFIND` (`Depth: infinity`), one request per month, and
  parse `d:href` and `d:getcontentlength`. Don't scrape the HTML index pages.
- Transfer with a small `Pool` of workers, each with its own `requests.Session`,
  streaming to `.part` (the gleam/imerg pattern), not `wget -r`.
- Config selectors: `product: hdo`, `processor_version: '0003'`, `year_range` (`null`
  means the whole record), and optionally `month_range` for small tests.
- `--dry-run` reports remote, present, missing, wrong-size and local-only counts and
  bytes by year.

## Remote: tropomi.grid.surfsara.nl (checked 2026-10-14)

- It is SRON's beta/research product server, hosted on SURF dCache. It is open data with
  **no login**. The README asks for co-authorship on publications (contacts
  t.borsdorff@sron.nl, j.landgraf@sron.nl). The README names the product
  "H2O/HDO (10_3)". The CH4 and CO products are "coming soon" and are not in scope.
- Tree: `/hdo/YYYY/MM/DD/<orbit>/s5p_l2_hdo_0003_<orbit, 5 digits>.nc`. There is one
  file per orbit, about 1.0–1.1 GB each, and about 14–15 orbits per day. The orbit
  directory name is not zero-padded (`373`), but the file name is (`00373`). `0003` is
  the processor version. Don't assume it is constant, and have the dry run report every
  version it sees.
- The record runs from 2017-11 to 2025-11. The `hdo/2025` directory was last modified
  2025-11-10, so it is unknown whether SRON is still adding data.
- WebDAV `PROPFIND` works (207 Multi-Status) at `Depth: 1` and `Depth: infinity`.
  `d:getcontentlength` matched a local file to the byte
  (`s5p_l2_hdo_0003_33931.nc` = 1,002,224,012). Collections report no content
  length. The server's README describes per-orbit tar files, but HDO is served as bare
  `.nc` (`application/x-netcdf`).
- Its throughput and rate limits are unknown. Measure them in the task 4 test and keep
  the worker count small.

## Local data (checked 2026-10-14)

- `/glade/derecho/scratch/kheyblom/data/wisosat/raw/TROPOMI/tropomi.grid.surfsara.nl/hdo/`
  (also reachable as `~/scratch/...`). It is **Lustre**, so use `lfs find` scoped to
  this subtree with a `-maxdepth`, never a broader path.
- 30,800 `.nc` files, about 30 TB, covering 2017-11 to 2024-05; 2024-06 onward is absent.
  Per year: 2017 402, 2018 4,510, 2019 5,094, 2020 4,827, 2021 5,107, 2022 4,827,
  2023 4,384, 2024 1,649. The rough gap is about 8,000 files, or about 8 TB, plus
  any holes in earlier years.
- The tree came from a mix of `wget -r` and an SMB copy from a Mac. That left stray
  files behind: `.DS_Store` files (at several levels), `2019/08/10/9453/.smbdelete*`,
  and two `*.nc?action=show` files (`2017/11/25/608/`, `2020/11/28/16210/`). The pipeline
  must ignore these: match only `s5p_l2_hdo_*.nc` exactly.
- Quota: scratch is 41 of 150 TiB used, which is ample. Derecho scratch is subject to
  purge, so a campaign copy may be needed eventually (TASKS.md *Later*).
- To check local state, stat the expected paths from the remote listing rather than
  walking the tree. Walk the tree (bounded depth) only for the local-only and stray
  report.

## Environment and commands

- Python 3.13 environment managed by `uv`, on a **uv-managed interpreter only**:
  `.python-version` is `3.13`, and `pyproject.toml` sets `[tool.uv]
  python-preference = "only-managed"` and `requires-python = ">=3.12,<3.14"`. uv's
  `cpython-3.13.12` is already installed under `~/.local/share/uv/python/`. Don't use
  3.14: numpy segfaults on it (see `../access_imerg/CLAUDE.md`). Run everything as
  `uv run python <script> --config config/<file>.yaml`.
- The likely dependencies are `requests` and `pyyaml` (used by `utils/path_utils.py`),
  plus `xarray`, `netcdf4`, `h5netcdf` and `jupyter` for checking files and for the
  search notebook. No auth library is needed.
- No build, lint or test tooling exists in this repo or its siblings. Verify with
  `--dry-run` and a `_tiny` config (one day, about 14 GB, into a separate scratch
  directory) before any real run.

## Sibling conventions to follow

`../access_imerg` is the most complete and most recent sibling. It is the model for
script structure, signal handling and run scripts. `../access_gleam` is the model for
the per-file `Pool` transfer.

Repo layout:
- `tropomi_download.py` (one entry-point script)
- `tropomi_search.ipynb` (a scratch notebook for browsing the remote)
- `config/config_download*.yaml` (one YAML per run variant, including a `_tiny` smoke-test one)
- `utils/log_utils.py` and `utils/path_utils.py`, copied from imerg (identical to
  mswep's; `setup_logging` takes a `fmt` arg).
- `logs/`, `run_download.sh`, `job_download.pbs`, `README.md`

Config shape: `directories.download`, `directories.logs` (`./logs`), `log_file`,
`n_processes` and the dataset selectors. `run_download.sh` extracts `log_file` and
`logs:` with `sed`, so keep those as top-level, simple scalar lines.

Script shape:
- A module docstring explains the remote layout, the local layout and restart behaviour.
- Google-style docstrings (`Args:`, `Returns:`, `Raises:`).
- Module-level compiled regexes carry a comment showing an example match.
- `LOG = logging.getLogger(__name__)`, and
  `LOG_FORMAT = '%(asctime)s [%(levelname)s] %(processName)s %(name)s: %(message)s'`.
- `main(settings, dry_run=False)` plus `argparse` with `--config` (required) and
  `--dry-run`.
- The final log line is `done :-)`.

Robustness (lessons carried over from imerg):
- Build the full file list and total size up front, and log them before any transfer.
- Make a one-byte ranged GET check (`check_access`) before starting the pool, so that a
  server problem fails fast and not once per file. Don't retry on 4xx; retry 5xx and
  connection errors with backoff.
- Write to `.part` and rename into place only after the size check. Sweep the run's
  stray `.part` files before and after a run and on interrupt.
- SIGINT/SIGTERM handlers in main: a background job started from a non-interactive
  shell inherits SIGINT as ignored. In `init_worker`, reset SIGTERM to `SIG_DFL` and
  ignore SIGINT, because on 3.13 the pool forks and workers inherit main's handlers.
- Write `logs/<stem>.pid` (`<host> <pid>`) once enumeration finishes, and remove it on
  exit. Refuse a second concurrent run over the same directory.
- Per-worker logs go to `<log stem>_<processname>.log`.
- Finish with a verification pass against the remote sizes that exits non-zero if
  anything is missing. A rerun picks up exactly those files.

Running (imerg/mswep pattern):
- `run_download.sh [config]` launches `setsid nohup nice -n 19 uv run python ...`,
  fully detached. `--status [config]` reads the pid file. Login nodes share GLADE but
  not process tables, so a run on another node has to be checked with `ssh <host>`.
- `job_download.pbs` is the fallback and targets **Casper** (`#PBS -q casper`). Derecho
  compute nodes have no outbound internet.

## Compute and allocation

The allocation is finite, so minimize core-hours. A download is network bound. The
sibling practice is a detached, `nice -n 19` run on a login node, with the Casper PBS
job as a fallback. That conflicts with the global rule that large downloads belong on
batch nodes, so **ask the user before starting any real download**, whichever route is
used. The task 6 gap is several TB and may take more than a day, so discuss the route
with the user. Always show any `qsub`/`qcmd` command and wait for approval.
`qsub -A "$PBS_ACCOUNT"` needs a login shell (`bash -lc`) for the variable to be set.

## Git

- `origin` fetches from `https://github.com/kheyblom/access_tropomi.git` but pushes
  over SSH (`git@github.com:kheyblom/access_tropomi.git`), because HTTPS has no
  credential helper on this system while the GitHub SSH key works. Set it with
  `git remote set-url --push origin git@github.com:kheyblom/access_tropomi.git`. Branch
  `main`.
- Commit each logical change as it is made. Use short imperative subjects, as in the
  siblings ("Add IMERG download pipeline and configs").
- Copy the sibling `.gitignore`. It excludes `*.log`, `*.out`, `nohup*`, `.venv`,
  `draft*` and `shards/`. Also ignore `logs/*.pid`. Track `pyproject.toml`, `uv.lock`
  and the search notebook, as the siblings do.
