We need to build a pipeline that downloads the TROPOMI L2 HDO data product (SRON's H2O/HDO product) from https://tropomi.grid.surfsara.nl/. Most of the record is already on disk at `/glade/derecho/scratch/kheyblom/data/wisosat/raw/TROPOMI/` (reached as `~/scratch/data/wisosat/raw/TROPOMI`). The pipeline only fetches the L2 HDO netCDF files that are on the remote but not on disk, and skips the files that are already there. We will work through each task one at a time together.

# 1. init CLAUDE.md for the codebase [COMPLETED]
Write a CLAUDE.md in the same shape as `../access_imerg/CLAUDE.md`: purpose, *Current working state*, environment and commands, a product/remote section (see *Remote and local facts* below), sibling conventions, compute and allocation, and git. Pull in the conventions and lessons recorded in the sibling repos, especially imerg (the most complete, and the most recent).

# 2. init a git repo [COMPLETED]
Initialize a git repo here and connect it to the remote (see general notes).
- `origin` fetches over HTTPS and pushes over SSH (`git@github.com:kheyblom/access_tropomi.git`), as the siblings do, because HTTPS has no credential helper on this system.
- branch `main`. Copy the sibling `.gitignore`. Add a `.gitignore` rule for anything the run writes into the repo.
- the remote is empty (checked 2026-10-14).

# 3. Plan and build the codebase [COMPLETED]
Build the pipeline to be consistent with the sibling access codebases: one `tropomi_download.py` driven by a YAML config, `utils/` copied from a sibling, `config/config_download.yaml` plus a `_tiny` one, `run_download.sh` (detached login-node run with `--status`), `job_download.pbs` (Casper fallback), `README.md`, a `tropomi_search.ipynb` scratch notebook, and a uv environment pinned to a uv-managed Python 3.13 (see imerg for why not 3.14).

Plan it with me before building. The proposed design:
- **listing**: the server is dCache WebDAV and needs no login. Enumerate the remote with `PROPFIND`, `Depth: infinity`, one request per month. This returns every file's path and exact byte size (`getcontentlength`), so the total volume is known before anything transfers. Don't scrape the HTML index pages.
- **transfer**: a small `Pool` of workers, each with its own `requests` session, streaming each file to `<file>.part` and renaming it into place once its size matches (the gleam/imerg pattern). Use a few workers, retries with backoff, and no retries on 4xx.
- **selection**: config keys `product` (`hdo`), `processor_version` (`0003`, the version in the file names), and `year_range` (`null` means the whole record). Optionally add a `month_range` for small tests.
- **local layout**: mirror the remote tree inside the existing directory, `<download>/tropomi.grid.surfsara.nl/hdo/YYYY/MM/DD/<orbit>/<file>.nc`, so that the files already on disk are recognized and nothing has to be moved. This differs from the sibling `<download>/<version>/raw/<product>/` layout. **Decided (2026-10-14): use this layout.**
- **skip rule**: skip a file whose local size equals the remote size. **Decided (2026-10-14)**: a local file whose size differs from the remote is re-downloaded (to `.part`, then renamed over the old file), as in the siblings.
- **never touch** files the run did not write: no deletes or moves in the existing tree beyond sweeping the run's own `.part` files.
- `--dry-run` reports remote files and bytes, files already present, files missing, files present with the wrong size, and local files with no remote counterpart, all broken down by year.
- end with a verification pass against the remote sizes. It exits non-zero if anything is missing, and a rerun picks up exactly those files.

Build (2026-10-14, design approved):
- `tropomi_download.py`, `config/config_download.yaml` (everything, into the existing tree) and `config/config_download_tiny.yaml` (2024-06-01 into `data/tropomi_tiny/`), `utils/`, `run_download.sh`, `job_download.pbs` (Casper fallback), `README.md`, `tropomi_search.ipynb`, and a uv venv (Python 3.13.12).
- selection is `date_range` (ISO dates, `null` = everything) rather than `year_range`, so the smoke test can be a single day.
- tiny dry run: 13 files, 12.29 GB, access check passed. Remote orbit directories sometimes hold processor `log*.txt`/`error*.txt` files and no `.nc`; these are ignored.
- full dry run (2026-10-14, ~25 min, login node, `nice -n 19`): 97 months (2017-11 to 2025-11), 38,648 files, 33.85 TB, all processor version 0003. 30,712 present, **7,936 to download (7.31 TB): 7,848 missing, 88 of the wrong size**. No warnings or errors. Access check passed. Full output: `logs/dryrun_full.out`.
- follow-up: the dry run now also lists missing files by month, every wrong-size file (local vs remote bytes), and stray files by kind.

# 4. Test the pipeline
Run the tiny config end-to-end before any real download. One day is ~14 files, ~14 GB, and goes into a separate scratch directory. Test on a login node, which costs no core-hours.
- `./run_download.sh config/config_download_tiny.yaml`. Check `--status`, the per-worker logs, a clean verification pass and `done :-)`.
- rerun it and confirm every file is skipped.
- interrupt a run with `kill -INT`. Confirm the `.part` files are swept, and that a rerun resumes and verifies.
- spot-check that a downloaded file opens with xarray/netCDF4.
- fix anything found, commit, and delete the test data (with my okay).

# 5. Audit the existing local data
Dry-run the real config against the existing tree, then report back before anything downloads (mostly done with the task 3 dry run; rerun the full dry run with the new per-month and wrong-size detail before task 6):
- missing files by year and month, with their total size, and a check that the quota covers it.
- local files whose size differs from the remote (possible truncated earlier transfers). Task 6 re-downloads these.
- stray files from earlier copies: `.DS_Store`, `.smbdelete*`, `*.nc?action=show` (seen 2026-10-14). Cleanup only with my approval.
- local files with no remote counterpart.

Results so far (2026-10-14/15):

| year | remote | present | missing | wrong size |
|---|---|---|---|---|
| 2017 | 402 | 401 | 0 | 1 |
| 2018 | 4,517 | 4,509 | 7 | 1 |
| 2019 | 5,095 | 5,093 | 1 | 1 |
| 2020 | 4,852 | 4,827 | 25 | 0 |
| 2021 | 5,107 | 5,107 | 0 | 0 |
| 2022 | 4,827 | 4,827 | 0 | 0 |
| 2023 | 4,795 | 4,300 | 411 | 84 |
| 2024 | 4,559 | 1,648 | 2,910 | 1 |
| 2025 | 4,494 | 0 | 4,494 | 0 |

- 2023 missing by month: 04 296 (a large hole), the rest 2–22 per month.
- the 2023 wrong-size files are **updated on the server, not truncated**: each is ~545–570 bytes smaller locally. Checked `2023/04/19/28570`: the remote is dated 2025-05-29 and its `history` reads "Last modified on 20250529T074909"; the local copy says 20240216. Re-downloading them is correct. Caveat: SRON edits files in place without a version bump, so an edit that leaves the size unchanged would not be detected.
- the 4 wrong-size files outside 2023 are not yet inspected.
- 0 local-only files. 198 stray files: mostly `.DS_Store`, plus `.smbdelete*` and `*.nc?action=show` (none in 2023 apart from 18 `.DS_Store`). **Cleanup needs my decision.**
- the server's orbit directories also hold SRON's own working files (`log*.txt`, `error*.txt`, `find_gap.py`, `nohup.out`, `rm.sh`): 1,309 in 190 orbits. These are ignored.

# 6. Download the missing files
Fetch everything the audit lists as missing or the wrong size into the existing tree.
- needs my approval before starting. This is likely several TB and may take more than a day.
- default route: `./run_download.sh` (detached, `nice -n 19` on a login node). Fallback: `qsub -A "$PBS_ACCOUNT" job_download.pbs` on Casper.
- monitor with `./run_download.sh --status`, and rerun if verification reports missing files.
- done when verification reports every remote file present locally at the right size.

# Later (not scheduled)
- keeping the record current with periodic top-up reruns, if SRON keeps adding data (the `hdo/2025` directory was last modified 2025-11-10).
- Derecho scratch is subject to purge. The record may eventually need a copy on campaign storage.

# Remote and local facts (checked 2026-10-14)
- remote: `https://tropomi.grid.surfsara.nl/hdo/YYYY/MM/DD/<orbit>/s5p_l2_hdo_0003_<orbit 5 digits>.nc`, one netCDF file per orbit, ~1.0–1.1 GB each, ~14–15 orbits per day. The record runs from 2017-11 to 2025-11. The README calls the product "H2O/HDO (10_3)". It is open data; SRON asks for co-authorship on publications (contact t.borsdorff@sron.nl, j.landgraf@sron.nl).
- WebDAV `PROPFIND` works (207) at `Depth: 1` and `Depth: infinity`. The remote size matched a local file to the byte (`s5p_l2_hdo_0003_33931.nc` = 1,002,224,012).
- local: `/glade/derecho/scratch/kheyblom/data/wisosat/raw/TROPOMI/tropomi.grid.surfsara.nl/hdo/` (Lustre), 30,800 `.nc` files, about 30 TB. It runs from 2017-11 to 2024-05; 2024-06 onward is absent. Files per year: 2017 402, 2018 4,510, 2019 5,094, 2020 4,827, 2021 5,107, 2022 4,827, 2023 4,384, 2024 1,649.
- rough gap estimate: about 8,000 files (~8 TB) for 2024-06 to 2025-11, plus any holes in earlier years. The dry run gives the real numbers.
- quota: scratch is 41 of 150 TiB used, so there is room.

# General notes:
- the git remote is: https://github.com/kheyblom/access_tropomi.git
- ensure to always use proper git version controlling as changes are made
- use and update a CLAUDE.md that will take this information and effectively and efficiently handle this project
- follow the same structure, workflow and rules as the other access pipelines in @/glade/u/home/kheyblom/work/data_access. `access_imerg` is the most complete and most recent.
- it is always important to minimize compute costs on derecho. we have a finite allocation and we need to managing our usage.
- keep notes on your current working state. you may lose connection to the HPC system or I may need to start new sessions, so I need you to be able to easily pick up where you left off.
- additional tasks may come up that need to occur between the above tasks. this task list can be flexible, but if substantial changes are need, I need to approve them.
- mark tasks as completed when they are completed
