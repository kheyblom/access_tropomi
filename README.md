# access_tropomi

Download SRON's TROPOMI L2 HDO (H2O/HDO) netCDF files from
https://tropomi.grid.surfsara.nl/ onto GLADE. Most of the record is already on disk,
so a run fetches only what is missing, plus any local file whose size differs from
the server's copy. It is structured like `access_imerg` and its siblings: one script
driven by one YAML config, a thin `utils/` layer, per-worker log files, and a run
that is safe to repeat.

```
tropomi_download.py               the downloader
tropomi_search.ipynb              scratch notebook for browsing the server and a file
config/config_download.yaml       everything, into the existing record
config/config_download_tiny.yaml  one day into a separate directory, for a smoke test
utils/                            config loading, logging
run_download.sh                   start the download on a login node
monitor_download.sh               watch the run's footprint on the node, stop it if it grows
job_download.pbs                  fallback batch job (Casper)
```

## One time setup

The server is open data, so no login is needed. Install the environment with
`uv sync`. It builds on a uv-managed Python 3.13 only, as in the sibling repos.

SRON asks for co-authorship on publications that use the product (see the
server's README.html).

## Running

```bash
# what would be downloaded, plus an audit of the local tree; no transfers
uv run python tropomi_download.py --config config/config_download.yaml --dry-run

# one day into a separate directory, as a smoke test
./run_download.sh config/config_download_tiny.yaml

# everything missing
./run_download.sh
```

`run_download.sh` starts the download under `setsid`, in a session of its own, so it
survives logout and anything that kills the starting shell's process group (`nohup`
alone only blocks SIGHUP). It runs at `nice -n 19` so it yields to anything
interactive on the shared node.

Once the listing finishes, the run records its host and pid in
`logs/<log_file stem>.pid`, and it deletes that file when it ends. The host matters:
Derecho's login nodes share GLADE but not their process tables.

```bash
./run_download.sh --status              # host, pid, whether it is alive
tail -n 40 logs/tropomi_download.log    # main log: totals, verification, failures
ls logs/                                 # one log per worker, one line per file
```

Stopping is always safe. `kill -INT <pid>` or a plain `kill <pid>` sweeps partial
files and removes the pid file on the way out, and a rerun picks up exactly what is
missing. A second run over the same download directory is refused while the first is
alive.

`run_download.sh` also starts `monitor_download.sh`, detached. It waits for the
run's pid file and then logs one line every 5 minutes to
`logs/<log_file stem>_monitor.log`. Each line gives the run's CPU and resident memory,
your user's memory against the login node's per-user cap (on Derecho, 8 CPUs and
10 GiB, with page cache from writing counted against the cap), the node's load and
free memory, and the run's progress and MB/s. If the run uses more than 2 cores or
2 GiB for two samples in a row, the monitor stops it with `kill -INT`, and a rerun
resumes. The limits can be set from the environment (`MAX_CORES`, `MAX_RSS_MB`,
`INTERVAL`, `BREACHES`). `--status` shows the latest line.

`job_download.pbs` is a fallback for the case where a login-node run keeps getting
killed. Submit it from the repo with `qsub -A "$PBS_ACCOUNT" job_download.pbs`. It
targets Casper, because Derecho compute nodes have no outbound internet.

## Config

| key | meaning |
|---|---|
| `directories.download` | the local root; files land under `<download>/tropomi.grid.surfsara.nl/` |
| `directories.logs`, `log_file` | where the main log, the worker logs and the pid file go |
| `n_processes` | parallel downloads; keep it small |
| `remote` | the server root, `https://tropomi.grid.surfsara.nl/` |
| `product` | the top-level directory on the server, `hdo` |
| `processor_version` | the version in the filename, `'0003'` (quoted, to keep the zeros) |
| `date_range` | inclusive `['YYYY-MM-DD', 'YYYY-MM-DD']`, or `null` for the whole record |
| `retries` | attempts per file before it is reported failed |

## How it works

The server is a dCache WebDAV door. It lays the product out as
`hdo/YYYY/MM/DD/<orbit>/s5p_l2_hdo_0003_<orbit>.nc`, one file of about 1 GB per
orbit and about 14 orbits a day. The orbit directory is not zero padded, but the
filename is. The run lists it with WebDAV `PROPFIND`: one request each for the
product and every year to find the months, then one `Depth: infinity` request per
month, which returns every file with its exact byte size. So the total volume is
known before anything transfers, and no per-file request is needed. Some orbit
directories also hold the processor's `log*.txt` and `error*.txt` files; these are
counted and ignored.

The local tree mirrors the server's, under a directory named for it:

```
<download>/tropomi.grid.surfsara.nl/hdo/2024/05/01/33931/s5p_l2_hdo_0003_33931.nc
```

This is where the existing record already sits, so the files on disk are recognized
in place. Each file is checked with one `stat`: if the local size matches the
server's, it is skipped; if it is missing or of a different size, it is downloaded.
Only those files go to the workers.

`n_processes` workers each stream their files over their own `requests` session. Each
file is written to `<file>.part`, checked against the expected byte count, and only
then renamed into place, over any older copy. A failed file is retried `retries`
times with backoff, except on a 4xx, before it is reported. Stray `.part` files in
the run's directories are swept before and after every run and on Ctrl-C. The run
ends with a verification pass over every listed file, and that pass exits non-zero if
anything is missing.

Nothing else on disk is touched. A `--dry-run` reports per year what is present,
missing or of the wrong size, and then audits the local tree. It lists product files
the server no longer has, stray files left by earlier copies (`.DS_Store`,
`.smbdelete*`, `*.nc?action=show`) and leftover `.part` files, and it changes none of
them.
