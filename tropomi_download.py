"""Download SRON TROPOMI L2 HDO netCDF files from the SURF dCache WebDAV server.

The server (https://tropomi.grid.surfsara.nl/, open data, no login) lays each
product out as ``<product>/<YYYY>/<MM>/<DD>/<orbit>/<file>.nc``, one file per
orbit, e.g. ``hdo/2024/05/01/33931/s5p_l2_hdo_0003_33931.nc``, where ``0003`` is
the processor version and the orbit is zero padded in the filename only. The
files are found with WebDAV PROPFIND requests rather than by scraping the HTML
index pages: one per product and one per year to find the months, then one
``Depth: infinity`` request per month, which yields every file's path and exact
byte size. The local tree mirrors the remote one under a directory named for the
server, ``<download>/tropomi.grid.surfsara.nl/<product>/YYYY/MM/DD/<orbit>/<file>``,
so that a record that is already on disk is picked up in place.

Which files are fetched is driven by the config: the ``product``, the
``processor_version`` and ``date_range`` (``null`` for the whole record). Files
are streamed by ``n_processes`` worker processes, each with its own HTTP session
and its own log file.

Downloads are restartable, and fetch only what is missing: a file whose local
copy already matches the remote size is skipped, and one of any other size is
fetched again. Transfers land on a ``.part`` file that is only renamed into
place, over any older copy, once complete, and stray ``.part`` files are swept
before and after a run and on Ctrl-C. Nothing else already on disk is touched.
The closing verification pass compares every expected file against the remote
size, so a rerun picks up exactly the files it lists. A ``--dry-run`` also
audits the local tree for files the remote does not list and for stray files
left by earlier copies, and reports them without changing anything.
"""

import logging
import argparse
import os
import re
import time
import signal
import socket
import multiprocessing
import xml.etree.ElementTree as ElementTree
from collections import Counter, defaultdict
from datetime import date
from multiprocessing import Pool
from urllib.parse import quote, unquote, urljoin, urlparse

from utils.path_utils import (
    load_config,
)
from utils.log_utils import (
    setup_logging,
)

import requests

# 'hdo/2024/05/01/33931/s5p_l2_hdo_0003_33931.nc', a file's path relative to the
# server root; the orbit directory is not zero padded, the filename is
PATH_RE = re.compile(
    r'^(?P<product>[^/]+)'
    r'/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})'
    r'/(?P<orbit>\d+)'
    r'/(?P<name>[^/]+)$'
)
# 's5p_l2_hdo_0003_33931.nc': product, processor version, orbit
FILENAME_RE = re.compile(
    r'^s5p_l2_(?P<product>[a-z0-9]+)'
    r'_(?P<version>\d{4})'
    r'_(?P<orbit>\d{5})'
    r'\.nc$'
)
# 'hdo/2024/' and 'hdo/2024/05/', directories one level down from a product
# and from a year
YEAR_DIR_RE = re.compile(r'^(?P<product>[^/]+)/(?P<year>\d{4})/$')
MONTH_DIR_RE = re.compile(r'^(?P<product>[^/]+)/(?P<year>\d{4})/(?P<month>\d{2})/$')
# ask only for what the listing uses; the default allprop answer is several
# times larger
PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    '<d:resourcetype/><d:getcontentlength/>'
    '</d:prop></d:propfind>'
)
DAV = '{DAV:}'
# processName keeps the workers apart in the shared console stream
LOG_FORMAT = '%(asctime)s [%(levelname)s] %(processName)s %(name)s: %(message)s'
# how many entries of each kind the dry run audit lists before summarizing
AUDIT_LIST_LIMIT = 50

LOG = logging.getLogger(__name__)

# per worker HTTP session and settings, filled in by init_worker; sessions
# cannot be shared between processes, so each worker keeps its own here
_WORKER_STATE = {}


def server_root(settings):
    """Local directory that mirrors the server root.

    Args:
        settings (dict): The loaded configuration.

    Returns:
        str: e.g. '<download>/tropomi.grid.surfsara.nl'.
    """
    host = urlparse(settings['remote']).netloc
    return os.path.join(settings['directories']['download'], host)


def product_root(settings):
    """Local directory that mirrors the configured product on the server.

    Args:
        settings (dict): The loaded configuration.

    Returns:
        str: e.g. '<download>/tropomi.grid.surfsara.nl/hdo'.
    """
    return os.path.join(server_root(settings), settings['product'])


def pid_file(settings):
    """Path of the file recording which host and pid are running the download.

    Args:
        settings (dict): The loaded configuration.

    Returns:
        str: e.g. '<logs>/tropomi_download.pid'.
    """
    stem, _ = os.path.splitext(settings['log_file'])
    return os.path.join(settings['directories']['logs'], f'{stem}.pid')


def write_pid_file(settings):
    """Record the host and pid of this run.

    A download started on one login node is invisible from the others -- GLADE
    is shared but process tables are not -- so the host has to be written down
    alongside the pid or a later session cannot find the run to check or stop it.

    Args:
        settings (dict): The loaded configuration.

    Returns:
        str: The path written.
    """
    path = pid_file(settings)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(f'{socket.gethostname()} {os.getpid()}\n')
    return path


def raise_interrupt(signum, frame):
    """Signal handler that turns SIGTERM into a KeyboardInterrupt.

    Args:
        signum (int): The signal received.
        frame (frame): The interrupted stack frame, unused.

    Raises:
        KeyboardInterrupt: Always, so the run cleans up as it would on Ctrl-C.
    """
    raise KeyboardInterrupt(f'received signal {signum}')


def install_signal_handlers():
    """Make SIGINT and SIGTERM stop the run cleanly.

    A job started in the background from a non-interactive shell, which is how
    run_download.sh starts it, inherits SIGINT as ignored, and Python leaves an
    ignored SIGINT alone, so without this ``kill -INT`` does nothing. SIGTERM,
    from a plain ``kill`` or a PBS walltime, would otherwise end the process
    without sweeping its partial files or removing the pid file.
    """
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, raise_interrupt)


def parse_date_range(date_range):
    """Turn the configured date range into a pair of dates.

    Args:
        date_range (list | None): Inclusive [first, last] as 'YYYY-MM-DD'
            strings (or dates, which is how YAML reads them unquoted), or None
            for the whole record.

    Returns:
        tuple | None: (first, last) as datetime.date, or None.

    Raises:
        ValueError: If the range is not two dates in order.
    """
    if date_range is None:
        return None
    if len(date_range) != 2:
        raise ValueError(f'date_range must be [first, last], got {date_range!r}')
    first, last = (
        value if isinstance(value, date) else date.fromisoformat(str(value))
        for value in date_range
    )
    if first > last:
        raise ValueError(f'date_range is reversed: {date_range!r}')
    return first, last


def month_in_range(year, month, date_range):
    """Whether any day of a month falls inside the date range.

    Args:
        year (int): The year.
        month (int): The month, 1-12.
        date_range (tuple | None): (first, last) dates, or None for no limit.

    Returns:
        bool: True if the month has to be listed.
    """
    if date_range is None:
        return True
    first, last = date_range
    return (first.year, first.month) <= (year, month) <= (last.year, last.month)


def day_in_range(day, date_range):
    """Whether a day falls inside the date range.

    Args:
        day (datetime.date): The day directory a file sits in.
        date_range (tuple | None): (first, last) dates, or None for no limit.

    Returns:
        bool: True if the file should be downloaded.
    """
    if date_range is None:
        return True
    first, last = date_range
    return first <= day <= last


def propfind(session, url, depth, retries):
    """Run one WebDAV PROPFIND and parse what it lists.

    Args:
        session (requests.Session): The session to send it on.
        url (str): The collection to list; it must end in '/'.
        depth (str): '1' for the collection's children, 'infinity' for the
            whole subtree.
        retries (int): Attempts before a connection error or server error is
            given up on. A 4xx is raised at once.

    Returns:
        list: (href, is_collection, size) triples, the collection itself
            included, with href the decoded absolute path on the server and size
            in bytes for files, None for collections.

    Raises:
        requests.HTTPError: If the server refused the request.
        RuntimeError: If the server answered with something other than a
            multistatus, e.g. because it does not speak WebDAV there.
    """
    headers = {'Depth': depth, 'Content-Type': 'application/xml; charset=utf-8'}
    for attempt in range(1, retries + 1):
        try:
            response = session.request(
                'PROPFIND', url, headers=headers, data=PROPFIND_BODY, timeout=(30, 600)
            )
            response.raise_for_status()
            break
        except requests.RequestException as error:
            status = getattr(getattr(error, 'response', None), 'status_code', None)
            if (status is not None and status < 500) or attempt == retries:
                raise
            LOG.warning(f'PROPFIND {url} failed ({error}), retrying')
            time.sleep(2**attempt)
    if response.status_code != 207:
        raise RuntimeError(f'PROPFIND {url} answered {response.status_code}, not 207')

    entries = []
    for item in ElementTree.fromstring(response.content).iter(f'{DAV}response'):
        href = unquote(item.findtext(f'{DAV}href') or '')
        is_collection = False
        size = None
        # dCache answers each property in a propstat of its own status, with
        # the ones that do not apply (a file's resourcetype, a directory's
        # length) under 404, so only the 200 ones are read
        for propstat in item.iter(f'{DAV}propstat'):
            status = (propstat.findtext(f'{DAV}status') or '').split()
            if len(status) < 2 or status[1] != '200':
                continue
            prop = propstat.find(f'{DAV}prop')
            if prop is None:
                continue
            if prop.find(f'{DAV}resourcetype/{DAV}collection') is not None:
                is_collection = True
            length = prop.findtext(f'{DAV}getcontentlength')
            if length:
                size = int(length)
        entries.append((href, is_collection, size))
    return entries


def relative_path(settings, href):
    """Path of a listed entry relative to the server root, for matching.

    Args:
        settings (dict): The loaded configuration.
        href (str): The decoded absolute path the server listed.

    Returns:
        str | None: e.g. 'hdo/2024/05/01/33931/s5p_l2_hdo_0003_33931.nc', or
            None if the entry is outside the configured server root or tries to
            climb out of it.
    """
    base = urlparse(settings['remote']).path.rstrip('/') + '/'
    if not href.startswith(base):
        return None
    relative = href[len(base):]
    if '..' in relative.split('/'):
        return None
    return relative


def list_months(session, settings, date_range):
    """List the month directories of the product that overlap the date range.

    Args:
        session (requests.Session): The session to list with.
        settings (dict): The loaded configuration.
        date_range (tuple | None): (first, last) dates, or None for no limit.

    Returns:
        list: (year, month) pairs, sorted.
    """
    product = settings['product']
    product_url = urljoin(settings['remote'], f'{product}/')
    years = []
    for href, is_collection, _ in propfind(session, product_url, '1', settings['retries']):
        match = YEAR_DIR_RE.match(relative_path(settings, href) or '')
        if is_collection and match and match.group('product') == product:
            years.append(int(match.group('year')))

    months = []
    for year in sorted(years):
        if date_range is not None and not date_range[0].year <= year <= date_range[1].year:
            continue
        year_url = urljoin(product_url, f'{year:04d}/')
        for href, is_collection, _ in propfind(session, year_url, '1', settings['retries']):
            match = MONTH_DIR_RE.match(relative_path(settings, href) or '')
            if not (is_collection and match and int(match.group('year')) == year):
                continue
            month = int(match.group('month'))
            if month_in_range(year, month, date_range):
                months.append((year, month))
    return sorted(months)


def list_month_files(session, settings, year, month, date_range, versions, others):
    """List the files to download from one month directory.

    Args:
        session (requests.Session): The session to list with.
        settings (dict): The loaded configuration.
        year (int): The year.
        month (int): The month.
        date_range (tuple | None): (first, last) dates, or None for no limit.
        versions (Counter): Tally of processor versions seen, updated in place
            with every product file listed, selected or not.
        others (list): Paths of listed files that are not product files at all,
            appended to in place. Some orbit directories hold the processor's
            'log0003_<orbit>_NN.txt' and 'error0003_<orbit>_NN.txt' instead of,
            or next to, a netCDF file.

    Returns:
        list: (url, local_path, size, day) tuples for the files of the
            configured product and processor version in the date range.
    """
    product = settings['product']
    month_url = urljoin(settings['remote'], f'{product}/{year:04d}/{month:02d}/')
    files = []
    for href, is_collection, size in propfind(session, month_url, 'infinity', settings['retries']):
        if is_collection:
            continue
        relative = relative_path(settings, href)
        path_match = PATH_RE.match(relative or '')
        if path_match is None:
            LOG.warning(f'skipping {href}: unexpected path')
            continue
        name_match = FILENAME_RE.match(path_match.group('name'))
        if name_match is None:
            others.append(href)
            continue
        # the name, its directory and the listing should all agree; a mismatch
        # would file an orbit under the wrong day or product without anyone
        # noticing
        if (
            name_match.group('product') != product
            or path_match.group('product') != product
            or int(name_match.group('orbit')) != int(path_match.group('orbit'))
            or (int(path_match.group('year')), int(path_match.group('month'))) != (year, month)
        ):
            LOG.warning(f'skipping {href}: filename does not match its path or {product}')
            continue
        versions[name_match.group('version')] += 1
        if name_match.group('version') != settings['processor_version']:
            continue
        day = date(
            int(path_match.group('year')), int(path_match.group('month')), int(path_match.group('day'))
        )
        if not day_in_range(day, date_range):
            continue
        if size is None:
            LOG.warning(f'skipping {href}: the server lists no size, so it cannot be verified')
            continue
        url = urljoin(settings['remote'], quote(href))
        local_path = os.path.join(server_root(settings), *relative.split('/'))
        files.append((url, local_path, size, day))
    return files


def list_files(settings, date_range):
    """List every file to download for the configured product.

    Args:
        settings (dict): The loaded configuration.
        date_range (tuple | None): (first, last) dates, or None for no limit.

    Returns:
        tuple: (files, months, versions, others), where files is a list of
            (url, local_path, size, day) tuples sorted by path, months the
            (year, month) pairs listed, versions a Counter of the processor
            versions seen, and others the paths of listed files that are not
            product files.
    """
    session = requests.Session()
    months = list_months(session, settings, date_range)
    LOG.info(
        f'listing {len(months)} months'
        + (f', {months[0][0]}-{months[0][1]:02d} to {months[-1][0]}-{months[-1][1]:02d}' if months else '')
    )
    files = []
    versions = Counter()
    others = []
    for year, month in months:
        month_files = list_month_files(
            session, settings, year, month, date_range, versions, others
        )
        LOG.info(f'  {year}-{month:02d}: {len(month_files)} files')
        files.extend(month_files)
    return sorted(files, key=lambda item: item[1]), months, versions, others


def local_status(local_path, size):
    """How the local copy of a file compares with the remote one.

    Args:
        local_path (str): Where the file belongs on disk.
        size (int): Its size on the server, in bytes.

    Returns:
        str: 'present' if the sizes match, 'missing' if there is no local file,
            or 'wrong_size' otherwise.
    """
    try:
        local_size = os.path.getsize(local_path)
    except FileNotFoundError:
        return 'missing'
    return 'present' if local_size == size else 'wrong_size'


def format_size(n_bytes):
    """Human readable file size.

    Args:
        n_bytes (int): Size in bytes.

    Returns:
        str: The size in MB, GB once it passes 1024 MB, or TB once it passes
            1024 GB.
    """
    mb = n_bytes / 1024**2
    if mb < 1024:
        return f'{mb:.1f} MB'
    if mb < 1024**2:
        return f'{mb / 1024:.2f} GB'
    return f'{mb / 1024**2:.2f} TB'


def log_status_by_year(files, statuses):
    """Log, per year, how many files are on disk, missing or of the wrong size.

    Args:
        files (list): (url, local_path, size, day) tuples.
        statuses (list): The local_status of each file, in the same order.
    """
    counts = defaultdict(Counter)
    sizes = defaultdict(Counter)
    for (_, _, size, day), status in zip(files, statuses):
        for key in ('remote', status):
            counts[day.year][key] += 1
            sizes[day.year][key] += size
    LOG.info('year: remote | present | missing | wrong size')
    for year in sorted(counts):
        LOG.info(
            f'{year}: {counts[year]["remote"]} ({format_size(sizes[year]["remote"])})'
            f' | {counts[year]["present"]}'
            f' | {counts[year]["missing"]} ({format_size(sizes[year]["missing"])})'
            f' | {counts[year]["wrong_size"]} ({format_size(sizes[year]["wrong_size"])})'
        )


def stray_kind(name):
    """Group a stray file by what most likely left it.

    Args:
        name (str): The file name.

    Returns:
        str: e.g. '.DS_Store' (macOS Finder), '.smbdelete*' (an SMB copy),
            '*?action=show' (wget following the server's 'show' links), or
            'other'.
    """
    if name == '.DS_Store':
        return '.DS_Store'
    if name.startswith('.smbdelete'):
        return '.smbdelete*'
    if '?action=' in name:
        return '*?action=show'
    return 'other'


def log_todo_details(files, statuses):
    """Log the missing files by month, and every file of the wrong size.

    Args:
        files (list): (url, local_path, size, day) tuples.
        statuses (list): The local_status of each file, in the same order.
    """
    missing = Counter(
        f'{day.year}-{day.month:02d}'
        for (_, _, _, day), status in zip(files, statuses) if status == 'missing'
    )
    if missing:
        LOG.info('missing by month: ' + ', '.join(f'{m} {n}' for m, n in sorted(missing.items())))
    wrong = [
        (local_path, size)
        for (_, local_path, size, _), status in zip(files, statuses) if status == 'wrong_size'
    ]
    LOG.info(f'{len(wrong)} files of the wrong size, to be fetched again')
    for local_path, size in wrong[:AUDIT_LIST_LIMIT]:
        local_size = os.path.getsize(local_path)
        LOG.info(
            f'  wrong size: {local_path} is {local_size} bytes, remote {size} '
            f'({100 * local_size / size:.1f}%)'
        )
    if len(wrong) > AUDIT_LIST_LIMIT:
        LOG.info(f'  ... and {len(wrong) - AUDIT_LIST_LIMIT} more')


def audit_local(settings, months, date_range, expected):
    """Report local files the run would neither download nor verify.

    Walks only the month directories the run listed (plus the files directly
    under the product and year directories), and changes nothing. Product files
    the server no longer lists are reported as local only; anything that is not
    a product file at all -- '.DS_Store', '.smbdelete*' or '*.nc?action=show'
    left by earlier copies -- is reported as stray; '.part' files are reported
    as partial, and are swept by the next real run in their directory.

    Args:
        settings (dict): The loaded configuration.
        months (list): The (year, month) pairs that were listed.
        date_range (tuple | None): (first, last) dates, or None for no limit.
        expected (set): Local paths of every file the remote lists.
    """
    root = product_root(settings)
    local_only, stray, partial = [], [], []

    def classify(path, name):
        if name.endswith('.part'):
            partial.append(path)
        elif FILENAME_RE.match(name):
            if path not in expected:
                local_only.append(path)
        else:
            stray.append(path)

    # files sitting directly in the product and year directories
    years = sorted({year for year, _ in months})
    for directory in [root] + [os.path.join(root, f'{year:04d}') for year in years]:
        if not os.path.isdir(directory):
            continue
        for entry in os.scandir(directory):
            if entry.is_file(follow_symlinks=False):
                classify(entry.path, entry.name)

    for year, month in months:
        month_dir = os.path.join(root, f'{year:04d}', f'{month:02d}')
        if not os.path.isdir(month_dir):
            continue
        for directory, _, filenames in os.walk(month_dir):
            # <month>/<DD>/<orbit>; a file above the day level has no day and is
            # always reported
            parts = os.path.relpath(directory, month_dir).split(os.sep)
            if parts[0] != '.' and parts[0].isdigit() and date_range is not None:
                try:
                    day = date(year, month, int(parts[0]))
                except ValueError:
                    day = None
                if day is not None and not day_in_range(day, date_range):
                    continue
            for name in filenames:
                classify(os.path.join(directory, name), name)

    kinds = Counter(stray_kind(os.path.basename(path)) for path in stray)
    if kinds:
        LOG.info(f'audit: stray files by kind: {dict(sorted(kinds.items()))}')
    # the Finder files are many and harmless, so only the others are listed
    stray = [path for path in stray if stray_kind(os.path.basename(path)) != '.DS_Store']
    for label, paths in (('local only', local_only), ('stray (not .DS_Store)', stray), ('partial', partial)):
        LOG.info(f'audit: {len(paths)} {label} files')
        for path in sorted(paths)[:AUDIT_LIST_LIMIT]:
            LOG.info(f'  {label}: {path}')
        if len(paths) > AUDIT_LIST_LIMIT:
            LOG.info(f'  ... and {len(paths) - AUDIT_LIST_LIMIT} more')


def check_access(url):
    """Fetch the first byte of one file to prove the server will hand it out.

    Checking one file up front turns a server that is down, or refusing this
    host, into one clear error instead of a failure per file.

    Args:
        url (str): The link of any file in the run.

    Raises:
        RuntimeError: If the request fails or the server refuses it.
    """
    try:
        with requests.get(url, headers={'Range': 'bytes=0-0'}, stream=True, timeout=(30, 300)) as response:
            if response.ok:
                return
            reason = f'{response.status_code} ({response.reason})'
    except requests.RequestException as error:
        reason = str(error)
    raise RuntimeError(f'could not fetch {url}: {reason}')


def worker_log_file(settings, worker=None):
    """Path of the per-process log file, for the current worker unless one is named.

    Args:
        settings (dict): The loaded configuration.
        worker (str, optional): Worker name to build a path for. Defaults to the
            name of the calling process.

    Returns:
        str: e.g. '<logs>/tropomi_download_forkpoolworker-1.log'.
    """
    stem, extension = os.path.splitext(settings['log_file'])
    worker = worker or multiprocessing.current_process().name.lower()
    return os.path.join(settings['directories']['logs'], f'{stem}_{worker}{extension}')


def init_worker(settings):
    """Give each worker process its own log file and HTTP session.

    Runs once per worker at pool startup, so each worker reuses one connection
    pool for every file it handles. A forked worker inherits the main process's
    signal handlers, so they are put back: the pool stops its workers with
    SIGTERM, which should simply end them, and an interrupt is the main
    process's to handle.

    Args:
        settings (dict): The loaded configuration.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    setup_logging(worker_log_file(settings), fmt=LOG_FORMAT)
    _WORKER_STATE['session'] = requests.Session()
    _WORKER_STATE['retries'] = settings['retries']
    LOG.info('worker ready')


def fetch(url, tmp_path):
    """Stream one URL to a local file over the worker's session.

    Args:
        url (str): The file's link.
        tmp_path (str): Where to write it.

    Returns:
        int: The number of bytes written.

    Raises:
        requests.HTTPError: If the server answered with an error status.
    """
    written = 0
    # connect timeout, then the longest wait allowed between two chunks
    with _WORKER_STATE['session'].get(url, stream=True, timeout=(30, 300)) as response:
        response.raise_for_status()
        with open(tmp_path, 'wb') as handle:
            for chunk in response.iter_content(chunk_size=4 * 1024**2):
                handle.write(chunk)
                written += len(chunk)
    return written


def download_file(job):
    """Download a single file, skipping it if a complete local copy already exists.

    A local copy of a different size is replaced: the new copy is written to a
    '.part' file and only renamed over the old one once it is complete.

    Args:
        job (tuple): (index, total, url, local_path, size); index and total are
            only used to tag the log lines.

    Returns:
        bool: True if the file is in place afterwards, False if every attempt
            failed.
    """
    index, total, url, local_path, size = job
    tag = f'[{index}/{total}]'
    name = os.path.basename(local_path)

    # the main process hands out only missing and wrong size files, but a
    # rerun of an interrupted pool may find this one done since
    status = local_status(local_path, size)
    if status == 'present':
        LOG.info(f'{tag} skipping {name}: already downloaded')
        return True

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    # transfer to a temporary name so an interrupted run never leaves a file
    # that looks complete to the skip check above
    tmp_path = local_path + '.part'
    attempts = _WORKER_STATE['retries']
    for attempt in range(1, attempts + 1):
        LOG.info(
            f'{tag} starting {url} ({format_size(size)}) -> {local_path}'
            + (' (replacing a copy of the wrong size)' if status == 'wrong_size' else '')
            + (f' (attempt {attempt}/{attempts})' if attempt > 1 else '')
        )
        start = time.monotonic()
        try:
            written = fetch(url, tmp_path)
            # a dropped connection can end the stream early without an error
            if written != size:
                raise IOError(f'got {written} bytes, the server lists {size}')
            os.replace(tmp_path, local_path)
        except Exception as error:
            # keep going with the other files; failures are reported at the end
            LOG.error(f'{tag} failed to download {name}: {error}')
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            response = getattr(error, 'response', None)
            if isinstance(error, requests.HTTPError) and response is not None \
                    and response.status_code < 500:
                # a refusal or a missing file, not a transient fault; a retry
                # only adds load
                return False
            if attempt < attempts:
                time.sleep(2**attempt)
            continue
        elapsed = time.monotonic() - start
        rate = size / 1024**2 / elapsed if elapsed else 0
        LOG.info(
            f'{tag} finished {name} ({format_size(size)}) '
            f'in {elapsed:.1f} s ({rate:.1f} MB/s)'
        )
        return True
    return False


def cleanup_partial_files(directories):
    """Remove leftover *.part files from a run that was killed mid-transfer.

    Only the directories this run writes to are looked at, one listing each,
    rather than a walk of the whole download tree. Note this removes every
    *.part in them, so two runs must not share a download directory.

    Args:
        directories (iterable): Local directories the run downloads into.

    Returns:
        int: Number of files removed.
    """
    removed = 0
    for directory in sorted(directories):
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if not filename.endswith('.part'):
                continue
            path = os.path.join(directory, filename)
            try:
                os.remove(path)
            except OSError as error:
                LOG.error(f'could not remove partial download {path}: {error}')
                continue
            LOG.warning(f'removed partial download {path}')
            removed += 1
    LOG.info(f'cleaned up {removed} partial downloads')
    return removed


def verify_downloads(files):
    """Check every expected file is on disk with the remote size, and log the results.

    Args:
        files (list): (url, local_path, size, day) tuples for every file the
            remote lists in the configured range, not only the ones this run
            fetched.

    Returns:
        bool: True if every file is present and complete.
    """
    LOG.info(f'verifying {len(files)} files')
    missing = []
    incomplete = []
    verified_bytes = 0
    for url, local_path, size, _ in files:
        if not os.path.exists(local_path):
            missing.append((url, local_path))
            continue
        local_size = os.path.getsize(local_path)
        if local_size != size:
            incomplete.append((url, local_path, local_size, size))
            continue
        verified_bytes += size

    # list the problem files first, then the summary line
    for url, local_path in missing:
        LOG.error(f'missing: {local_path} (remote {url})')
    for url, local_path, local_size, size in incomplete:
        LOG.error(
            f'size mismatch: {local_path} is {local_size} bytes, '
            f'remote {url} is {size}'
        )

    n_ok = len(files) - len(missing) - len(incomplete)
    LOG.info(
        f'verified {n_ok}/{len(files)} files ({format_size(verified_bytes)}), '
        f'{len(missing)} missing, {len(incomplete)} incomplete'
    )
    return not missing and not incomplete


def main(settings, dry_run=False):

    install_signal_handlers()
    os.makedirs(settings['directories']['logs'], exist_ok=True)
    log_file = os.path.join(settings['directories']['logs'], settings['log_file'])
    setup_logging(log_file, fmt=LOG_FORMAT)

    date_range = parse_date_range(settings['date_range'])
    dates = (
        f'{date_range[0].isoformat()} to {date_range[1].isoformat()}' if date_range else 'all'
    )
    LOG.info(
        f'downloading TROPOMI {settings["product"]} processor version '
        f'{settings["processor_version"]} (dates: {dates}) from {settings["remote"]} '
        f'into {product_root(settings)}'
    )
    LOG.info(f'running as pid {os.getpid()} on {socket.gethostname()}')

    # build the full file list up front, so the total volume is known before
    # any transfer starts
    files, months, versions, others = list_files(settings, date_range)
    LOG.info(f'processor versions seen: {dict(sorted(versions.items()))}')
    # the processor's own log and error files, not data; counted, not fetched
    if others:
        orbits = {os.path.dirname(href) for href in others}
        LOG.info(
            f'ignored {len(others)} non-product files in {len(orbits)} orbit directories, '
            f'e.g. {", ".join(sorted(others)[:3])}'
        )
    total_bytes = sum(size for _, _, size, _ in files)
    LOG.info(f'found {len(files)} files on the server ({format_size(total_bytes)})')

    # compare with what is already on disk, one stat per file
    statuses = [local_status(local_path, size) for _, local_path, size, _ in files]
    log_status_by_year(files, statuses)
    todo = [item for item, status in zip(files, statuses) if status != 'present']
    n_wrong = statuses.count('wrong_size')
    todo_bytes = sum(size for _, _, size, _ in todo)
    LOG.info(
        f'{statuses.count("present")} files already present, {len(todo)} to download '
        f'({statuses.count("missing")} missing, {n_wrong} of the wrong size), '
        f'{format_size(todo_bytes)}'
    )

    # one byte of one file, so a server problem stops the run here, dry run
    # included, rather than failing every file in turn
    if files:
        try:
            check_access((todo or files)[0][0])
        except RuntimeError as error:
            LOG.error(str(error))
            raise SystemExit(1)
        LOG.info('access check passed')

    if dry_run:
        log_todo_details(files, statuses)
        for url, local_path, size, _ in todo[:3]:
            LOG.info(f'  {url} ({format_size(size)}) -> {local_path}')
        if len(todo) > 3:
            LOG.info(f'  ... and {len(todo) - 3} more')
        audit_local(settings, months, date_range, {local_path for _, local_path, _, _ in files})
        LOG.info('dry run, nothing downloaded')
        return

    if not files:
        LOG.warning('no files matched the configuration, nothing to download')
        return

    if todo:
        # number the jobs so each log line says which file of how many it is
        jobs = [
            (index, len(todo), url, local_path, size)
            for index, (url, local_path, size, _) in enumerate(todo, start=1)
        ]
        directories = {os.path.dirname(local_path) for _, _, _, local_path, _ in jobs}

        # clear debris from any previous run before the workers start writing
        cleanup_partial_files(directories)
        LOG.info(f'wrote {write_pid_file(settings)}')

        n_processes = min(settings['n_processes'], len(jobs)) or 1
        LOG.info(
            f'starting {n_processes} worker processes, '
            f'each logging to {worker_log_file(settings, worker="<worker>")}'
        )
        try:
            # chunksize=1 hands out one file at a time, so a worker that draws a
            # slow file does not hold up a whole pre-assigned block
            with Pool(processes=n_processes, initializer=init_worker, initargs=(settings,)) as pool:
                results = pool.map(download_file, jobs, chunksize=1)
        except BaseException as error:
            # covers ctrl-c too; the pool has terminated and joined its workers by
            # the time this runs, so nothing is still writing to a .part file
            LOG.error(f'download interrupted ({type(error).__name__}), cleaning up before exiting')
            cleanup_partial_files(directories)
            raise
        finally:
            # a stale pid file would point at a process that is gone, or worse at a
            # pid the system has since reused
            if os.path.exists(pid_file(settings)):
                os.remove(pid_file(settings))
        cleanup_partial_files(directories)

        for (index, _, url, _, _), ok in zip(jobs, results):
            if not ok:
                LOG.error(f'[{index}/{len(jobs)}] did not download: {url}')
        LOG.info(f'downloaded {results.count(True)} files, {results.count(False)} failed')
    else:
        LOG.info('nothing to download, every file is already present')

    # final check of every listed file against the remote sizes, in the main
    # log; a rerun picks up exactly the files reported here
    if not verify_downloads(files):
        LOG.error('download incomplete, rerun to retry the files listed above')
        raise SystemExit(1)

    LOG.info('all files downloaded and verified')
    LOG.info('done :-)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='download sron tropomi l2 hdo files from the surf dcache webdav server.'
    )
    parser.add_argument(
        '--config',
        type=str,
        required=True,
        help='Path to YAML configuration file.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='List what would be downloaded, audit the local tree, and exit.',
    )
    args = parser.parse_args()
    settings = load_config(args.config)
    main(settings, dry_run=args.dry_run)
