#!/usr/bin/env python3
"""Minimal cron scheduler.

Usage: scheduler.py "<5-field cron expression>" <command> [args...]

Supported per field: *  number  range a-b  list a,b,c  step */n, a-b/n, a/n
Fields: minute hour day-of-month month day-of-week (0 and 7 = Sunday).
If both day-of-month AND day-of-week are restricted, either one matching is
enough, as in cron.
Runs never overlap: a slot that comes up while the command is still running
is skipped.
"""
import datetime
import signal
import subprocess
import sys
import time

FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("day-of-month", 1, 31), ("month", 1, 12), ("day-of-week", 0, 7)]


def parse_field(expr, name, lo, hi):
    values = set()
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError(f"{name}: step must be >= 1")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
        else:
            start = int(part)
            end = hi if step > 1 else start
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ValueError(f"{name}: '{part}' is outside {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return values


def parse(expression):
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError(f"cron expression needs 5 fields, got: '{expression}'")
    sets = [parse_field(p, *f) for p, f in zip(parts, FIELDS)]
    if 7 in sets[4]:
        sets[4] = (sets[4] - {7}) | {0}
    # as in cron: a field starting with * counts as "unrestricted"
    dom_any = parts[2].startswith("*")
    dow_any = parts[4].startswith("*")
    return sets, dom_any, dow_any


def matches(spec, dt):
    (minutes, hours, doms, months, dows), dom_any, dow_any = spec
    if dt.minute not in minutes or dt.hour not in hours or dt.month not in months:
        return False
    dom_ok = dt.day in doms
    dow_ok = (dt.isoweekday() % 7) in dows
    if dom_any or dow_any:
        return dom_ok and dow_ok
    return dom_ok or dow_ok


def next_run(spec, after):
    dt = after.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    for _ in range(366 * 24 * 60 * 5):  # search at most ~5 years ahead
        if matches(spec, dt):
            return dt
        dt += datetime.timedelta(minutes=1)
    raise ValueError("cron expression never matches")


def log(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} scheduler: {msg}", flush=True)


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: scheduler.py '<cron>' <command> [args...]")
    expression, command = sys.argv[1], sys.argv[2:]
    try:
        spec = parse(expression)
        upcoming = next_run(spec, datetime.datetime.now())
    except ValueError as exc:
        sys.exit(f"invalid schedule: {exc}")

    child = None

    def stop(signum, _frame):
        log(f"received signal {signum}, exiting")
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log(f"schedule '{expression}', next run {upcoming:%Y-%m-%d %H:%M}")
    while True:
        wait = (upcoming - datetime.datetime.now()).total_seconds()
        if wait > 0:
            # sleep in chunks so clock jumps (DST, NTP) do not cause trouble
            time.sleep(min(wait, 60))
            continue
        child = subprocess.Popen(command)
        rc = child.wait()
        child = None
        if rc != 0:
            log(f"command exited with status {rc}")
        upcoming = next_run(spec, datetime.datetime.now())
        log(f"next run {upcoming:%Y-%m-%d %H:%M}")


if __name__ == "__main__":
    main()
