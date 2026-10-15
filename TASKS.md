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

# 4. Test the pipeline [COMPLETED]
Run the tiny config end-to-end before any real download. One day is ~14 files, ~14 GB, and goes into a separate scratch directory. Test on a login node, which costs no core-hours.
- `./run_download.sh config/config_download_tiny.yaml`. Check `--status`, the per-worker logs, a clean verification pass and `done :-)`.
- rerun it and confirm every file is skipped.
- interrupt a run with `kill -INT`. Confirm the `.part` files are swept, and that a rerun resumes and verifies.
- spot-check that a downloaded file opens with xarray/netCDF4.
- fix anything found, commit, and delete the test data (with my okay).

Results (2026-10-15, login node derecho1, 2 workers):
- run: about 50 MB/s per worker (~100 MB/s total), about 20 s per ~1 GB file. No errors.
- `kill -INT` after 8 of 13 files: both in-flight `.part` files were swept, the pid file was removed, and no processes were left.
- resume: 8 present, 5 downloaded, verified 13/13 (12.29 GB), `done :-)`.
- wrong-size path: trimmed one file by 549 bytes (like SRON's rewrites). The rerun re-fetched only that file, restored its size, and verified 13/13.
- rerun: all 13 present, nothing to download, verified, `done :-)`.
- files open with xarray (`group='target_product'`, `'instrument'`): `nobs` 897,195, `deltad` finite, latitudes -78 to 90.
- nothing needed fixing. The test data (`data/tropomi_tiny/`, 12.3 GB) is still on disk until I okay deleting it.

# 5. Audit the existing local data [COMPLETED]
Dry-run the real config against the existing tree, then report back before anything downloads (mostly done with the task 3 dry run; rerun the full dry run with the new per-month and wrong-size detail before task 6):
- missing files by year and month, with their total size, and a check that the quota covers it.
- local files whose size differs from the remote (possible truncated earlier transfers). Task 6 re-downloads these.
- stray files from earlier copies: `.DS_Store`, `.smbdelete*`, `*.nc?action=show` (seen 2026-10-14). Cleanup only with my approval. **Removed on 2026-10-15 at my request**: 200 files (197 `.DS_Store`, 1 `.smbdelete*`, 2 `*?action=show`), deleted from an explicit list. The tree now holds only `s5p_l2_hdo_*.nc` files.
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
- the wrong-size files outside 2023 are **truncated** copies: 2017/11/16/482 at 23.5%, 2018/06/21/3567 at 98.8%, 2019/02/15/6953 at 99.7% (the 2024 one is past the listing limit). All are fetched again.
- rerun of the full dry run (2026-10-15 09:03–09:25): the same totals; 0 stray files after the cleanup. Missing by month: 2018-08 7, 2019-05 1, 2020-09 25, 2023-02 12, 2023-03 2, 2023-04 296, 2023-05 17, 2023-06 22, 2023-07 8, 2023-08 12, 2023-09 7, 2023-10 11, 2023-11 16, 2023-12 8, 2024-01 37, 2024-02 81, 2024-03 86, 2024-04 79, 2024-06 383, 2024-07 348, 2024-08 434, 2024-09 406, 2024-10 324, 2024-11 310, 2024-12 422, 2025-01 379, 2025-02 372, 2025-03 410, 2025-04 398, 2025-05 429, 2025-06 404, 2025-07 427, 2025-08 429, 2025-09 416, 2025-10 423, 2025-11 407.
- 0 local-only files. 198 stray files: mostly `.DS_Store`, plus `.smbdelete*` and `*.nc?action=show` (none in 2023 apart from 18 `.DS_Store`). **Cleanup needs my decision.**
- the server's orbit directories also hold SRON's own working files (`log*.txt`, `error*.txt`, `find_gap.py`, `nohup.out`, `rm.sh`): 1,309 in 190 orbits. These are ignored.

# 6. Download the missing files [IN PROGRESS]
Fetch everything the audit lists as missing or the wrong size into the existing tree.
- needs my approval before starting. This is likely several TB and may take more than a day.
- default route: `./run_download.sh` (detached, `nice -n 19` on a login node). Fallback: `qsub -A "$PBS_ACCOUNT" job_download.pbs` on Casper.
- monitor with `./run_download.sh --status`, and rerun if verification reports missing files.
- done when verification reports every remote file present locally at the right size.

Run (approved 2026-10-15: login node, monitored):
- started 2026-10-15 ~09:27 on derecho1 with `./run_download.sh` (4 workers, `nice -n 19`), together with `monitor_download.sh`, which logs every 5 min to `logs/tropomi_download_monitor.log` and stops the run at > 2 cores or > 2 GiB RSS. The login node caps the user at 8 CPUs and 10 GiB.
- expected: transfers start ~09:45 after the listing. 7.31 TB at 100–200 MB/s is 10–21 h.
- first samples (09:44–09:49): 0.5–0.6 cores, ~340 MB RSS, 148–161 MB/s; node load ~5–6, 270 GB free. Estimated finish ~23:00–23:30 2026-10-15. The user cgroup sits at its 10 GiB cap, but ~4.5 GB of that is inactive page cache, and there are 0 OOM kills.
- server-side failure: `2018/08/18/4379` answered 500 after ~3 min hanging, on all 3 attempts (and a direct curl timed out). Probably a dCache pool or file that is unavailable, which is likely also why it was missing locally. Expect the other missing files from 2017–2020 (33) to behave the same. They will be listed in the final verification. If a rerun cannot get them either, email SRON (t.borsdorff@sron.nl) with the list.
- **2026-10-15 ~12:10: `2018/08/18/4379` became downloadable later.** I (the user) downloaded it manually in a browser. A one-byte curl at 12:12 got 206 in 3.4 s. The same probe on `2024/06/22/34680` (failed ~12:00) still hung for 90 s. Working hypothesis: these files are on tape in dCache, the first requests trigger staging, and staging takes longer than our 3 attempts (~9 min). Once staged, the file serves normally. **If these files fail again:** they are probably not missing or corrupt on the server. Wait (hours), then rerun; only the failed files are retried. Contact SRON only if a later rerun still fails. Option for later: a much longer backoff (minutes) on 5xx so that a run can wait out staging. **Declined for now (2026-10-15): keep the retry behavior as is.**
- failures as of 12:05 (all 500 on 3 attempts, about 0.6% of files tried): `2018/08/18/4379` (now served), `2023/04/19/28571`, `2023/05/09/28860`, `2024/02/21/32949`, `2024/03/05/33130`, `2024/03/14/33256`, `2024/03/17/33302`, `2024/03/30/33486`, `2024/06/22/34680`. 1,552/7,936 done, ~196 MB/s, finish now estimated ~21:00.

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
