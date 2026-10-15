#!/bin/bash
# Watch a running download's footprint on the shared login node, and stop it if
# it grows past what a download should need.
#
# Every INTERVAL seconds it logs one line: the download's CPU (cores, from the
# change in its processes' cumulative CPU time) and resident memory, this user's
# cgroup memory (Derecho login nodes cap each user, e.g. 8 CPUs and 10 GiB, and
# page cache from writing counts against that cap, so it is shared with an
# editor session and anything else running), the node's load and available
# memory, and the download's progress and throughput from the worker logs.
#
# If the download's CPU or resident memory is over the limit for BREACHES
# samples in a row, it is stopped with SIGINT, which sweeps partial files and
# removes the pid file; a rerun resumes. The monitor exits when the pid file
# goes away, i.e. when the run ends.
#
# Usage (run_download.sh starts it; by hand it is):
#   setsid nohup ./monitor_download.sh [config] > /dev/null 2>&1 < /dev/null &
# Limits can be overridden from the environment: INTERVAL (seconds, default
# 300), MAX_CORES (default 2), MAX_RSS_MB (default 2048), BREACHES (default 2),
# WAIT_START (seconds to wait for the pid file to appear, default 7200).

set -uo pipefail

cd "$(dirname "$0")"

CONFIG="${1:-config/config_download.yaml}"
INTERVAL="${INTERVAL:-300}"
MAX_CORES="${MAX_CORES:-2}"
MAX_RSS_MB="${MAX_RSS_MB:-2048}"
BREACHES="${BREACHES:-2}"
WAIT_START="${WAIT_START:-7200}"

LOG_FILE=$(sed -n 's/^log_file:[[:space:]]*//p' "${CONFIG}" | head -1)
LOG_DIR=$(sed -n 's/^[[:space:]]*logs:[[:space:]]*//p' "${CONFIG}" | head -1)
STEM="${LOG_FILE%.log}"
PID_FILE="${LOG_DIR}/${STEM}.pid"
MONITOR_LOG="${LOG_DIR}/${STEM}_monitor.log"
MEM_CG="/sys/fs/cgroup/memory/user.slice/user-$(id -u).slice"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "${MONITOR_LOG}"
}

# the enumeration takes a while before the run writes its pid file; a pid file
# older than this monitor is a stale one from a run that died
MONITOR_START=$(date +%s)
waited=0
while [[ ! -f "${PID_FILE}" ]] || (( $(stat -c %Y "${PID_FILE}") < MONITOR_START - 5 )); do
    if (( waited >= WAIT_START )); then
        log "no ${PID_FILE} after ${WAIT_START} s, giving up"
        exit 1
    fi
    sleep "${POLL_START:-60}"
    waited=$(( waited + ${POLL_START:-60} ))
done

read -r RUN_HOST RUN_PID < "${PID_FILE}"
if [[ "${RUN_HOST}" != "$(hostname)" ]]; then
    log "run is on ${RUN_HOST}, not $(hostname); a monitor has to run on the same node"
    exit 1
fi
# the workers and the uv wrapper share the session run_download.sh's setsid made
SID=$(ps -o sid= -p "${RUN_PID}" | tr -d ' ')
log "monitoring pid ${RUN_PID} (session ${SID}) on ${RUN_HOST}: every ${INTERVAL} s," \
    "stop at > ${MAX_CORES} cores or > ${MAX_RSS_MB} MB RSS for ${BREACHES} samples"

cpu_seconds() {
    # cumulative CPU time of every process in the session, in seconds
    ps -s "${SID}" -o times= 2>/dev/null | awk '{ s += $1 } END { print s + 0 }'
}

finished_mb() {
    # megabytes of files finished by this run's workers; the GB/MB unit is in
    # the log line, e.g. 'finished s5p_l2_hdo_0003_34371.nc (947.9 MB) in ...'
    # fork or forkserver, depending on the start method; named exactly, so another
    # config's logs (e.g. <stem>_tiny_...) are never counted
    cat "${LOG_DIR}/${STEM}"_forkpoolworker-*.log "${LOG_DIR}/${STEM}"_forkserverpoolworker-*.log 2>/dev/null \
        | awk -v since="${START_STAMP}" '
            $1 " " $2 >= since && / finished / {
                n++
                match($0, /\(([0-9.]+) (MB|GB)\)/, m)
                mb += (m[2] == "GB") ? m[1] * 1024 : m[1]
            }
            END { printf "%d %.0f\n", n, mb }'
}

START_STAMP=$(date '+%Y-%m-%d %H:%M:%S')
prev_cpu=$(cpu_seconds)
prev_mb=0
prev_time=$(date +%s)
strikes=0

while kill -0 "${RUN_PID}" 2>/dev/null && [[ -f "${PID_FILE}" ]]; do
    sleep "${INTERVAL}"
    kill -0 "${RUN_PID}" 2>/dev/null || break

    now=$(date +%s)
    elapsed=$(( now - prev_time ))
    cpu=$(cpu_seconds)
    cores=$(awk -v a="${cpu}" -v b="${prev_cpu}" -v t="${elapsed}" 'BEGIN { printf "%.2f", (a - b) / t }')
    rss_mb=$(ps -s "${SID}" -o rss= 2>/dev/null | awk '{ s += $1 } END { printf "%d", s / 1024 }')
    n_procs=$(ps -s "${SID}" -o pid= 2>/dev/null | wc -l)

    cg_used=$(( $(cat "${MEM_CG}/memory.usage_in_bytes" 2>/dev/null || echo 0) / 1024**2 ))
    cg_limit=$(( $(cat "${MEM_CG}/memory.limit_in_bytes" 2>/dev/null || echo 0) / 1024**2 ))
    cg_cache=$(awk '$1 == "cache" { printf "%d", $2 / 1024^2 }' "${MEM_CG}/memory.stat" 2>/dev/null)
    load=$(cut -d' ' -f1-3 /proc/loadavg)
    node_avail_gb=$(awk '/^MemAvailable/ { printf "%d", $2 / 1024^2 }' /proc/meminfo)

    read -r n_done mb_done < <(finished_mb)
    rate=$(awk -v a="${mb_done}" -v b="${prev_mb}" -v t="${elapsed}" 'BEGIN { printf "%.0f", (a - b) / t }')

    log "cpu ${cores} cores | rss ${rss_mb} MB (${n_procs} procs)" \
        "| user cgroup ${cg_used}/${cg_limit} MB (cache ${cg_cache:-?} MB)" \
        "| node load ${load}, ${node_avail_gb} GB available" \
        "| ${n_done} files, $(( mb_done / 1024 )) GB done this run, ${rate} MB/s"

    over=$(awk -v c="${cores}" -v r="${rss_mb}" -v mc="${MAX_CORES}" -v mr="${MAX_RSS_MB}" \
        'BEGIN { print (c > mc || r > mr) ? 1 : 0 }')
    if (( over )); then
        strikes=$(( strikes + 1 ))
        log "WARNING over the limit (${strikes}/${BREACHES})"
        if (( strikes >= BREACHES )); then
            log "STOPPING the download: kill -INT ${RUN_PID}; rerun ./run_download.sh to resume"
            kill -INT "${RUN_PID}"
            exit 2
        fi
    else
        strikes=0
    fi

    prev_cpu="${cpu}"
    prev_mb="${mb_done}"
    prev_time="${now}"
done

log "run ${RUN_PID} has ended; last main log line: $(tail -n 1 "${LOG_DIR}/${LOG_FILE}")"
