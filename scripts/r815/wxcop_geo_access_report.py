#!/usr/bin/env python3
"""Summarize where CAP WxCOP web traffic is coming from, geolocated by country
and enriched with RDAP org/location, cross-referenced against fail2ban's
banned/blacklisted IPs.

Reads the Apache combined-format access log for the wxcop vhost, aggregates
hits per client IP over a lookback window, geolocates each IP using the local
GeoIP legacy country database (geoip-database / geoiplookup, already
installed - no API key or network calls required), enriches each external IP
with organization/location detail via a public RDAP lookup (rdap.arin.net,
free, no key, resolves globally via RIR referral), and flags any hit IP that
fail2ban has also seen as a threat (currently banned in any jail, or
permanently listed in /etc/fail2ban/blacklist.txt).

Known/internal IPs (KNOWN_IPS below) skip geolocation and RDAP entirely and
are reported separately - no need to re-explain traffic from a known network
on every run.

build_report() returns a plain-dict/list structure (JSON-serializable) so
this can be reused by a web endpoint (see wxcop_geo_admin.py), not just the
CLI. Run with sudo - reading the Apache log needs the 'adm' group, and
fail2ban-client needs root.
"""
import argparse
import ast
import gzip
import grp
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address

import requests

LOG_DIR = "/var/log/apache2"
LOG_BASENAME = "cap-wxcop-ssl-access.log"
BLACKLIST_FILE = "/etc/fail2ban/blacklist.txt"

# IPs we already know the origin of - skip geolocation/RDAP, report separately.
KNOWN_IPS = {
    "209.248.104.165": "CAP HQ (known network)",
}

LOG_LINE_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"(?P<request>[^"]*)" (?P<status>\d+) (?P<size>\S+) '
    r'"(?P<referer>[^"]*)" "(?P<agent>[^"]*)"'
)
APACHE_TIME_FMT = "%d/%b/%Y:%H:%M:%S %z"

_geoip_cache = {}
_rdap_cache = {}


def is_private(ip_str):
    try:
        ip = ip_address(ip_str)
    except ValueError:
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local


def geolocate(ip_str):
    if ip_str in _geoip_cache:
        return _geoip_cache[ip_str]
    try:
        out = subprocess.run(
            ["geoiplookup", ip_str], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        # "GeoIP Country Edition: US, United States"
        m = re.search(r":\s*([A-Z]{2}),\s*(.+)$", out)
        result = (m.group(1), m.group(2)) if m else ("??", "Unknown")
    except Exception:
        result = ("??", "Unknown")
    _geoip_cache[ip_str] = result
    return result


def rdap_lookup(ip_str):
    """Best-effort org name + city/state/country via RDAP (rdap.arin.net,
    free, no key - referred to the correct regional registry automatically).
    Returns (org, location) - either may be None on failure or missing data."""
    if ip_str in _rdap_cache:
        return _rdap_cache[ip_str]
    org, location = None, None
    try:
        resp = requests.get(
            f"https://rdap.arin.net/registry/ip/{ip_str}", timeout=5,
            headers={"Accept": "application/rdap+json"},
        )
        if resp.ok:
            data = resp.json()
            for entity in data.get("entities", []):
                vcard = entity.get("vcardArray")
                if not vcard or len(vcard) < 2:
                    continue
                for item in vcard[1]:
                    if item[0] == "fn" and not org:
                        org = item[3]
                    elif item[0] == "adr" and not location:
                        label = item[1].get("label") if len(item) > 1 else None
                        if label:
                            location = " ".join(l.strip() for l in label.splitlines() if l.strip())
                if org and location:
                    break
    except Exception:
        pass  # RDAP unreachable / rate-limited / malformed - degrade gracefully
    result = (org, location)
    _rdap_cache[ip_str] = result
    return result


def get_fail2ban_threats():
    """Return {ip: sorted [source, ...]} for every IP fail2ban currently has
    banned in any jail, plus everything in the standing blacklist.txt."""
    threats = defaultdict(set)
    try:
        out = subprocess.run(
            ["fail2ban-client", "banned"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        for jail_map in ast.literal_eval(out):
            for jail, ips in jail_map.items():
                for ip in ips:
                    threats[ip].add(jail)
    except Exception:
        pass  # fail2ban not reachable (not run as root, or not installed) - degrade gracefully

    try:
        with open(BLACKLIST_FILE) as f:
            for line in f:
                ip = line.strip()
                if ip and not ip.startswith("#"):
                    threats[ip].add("blacklist.txt")
    except OSError:
        pass

    return {ip: sorted(sources) for ip, sources in threats.items()}


def candidate_log_files(cutoff):
    """Yield (path, opener) for the live log plus rotated logs, newest first,
    stopping once a rotated file's mtime is well before the cutoff."""
    yield f"{LOG_DIR}/{LOG_BASENAME}", open
    n = 1
    while True:
        plain = f"{LOG_DIR}/{LOG_BASENAME}.{n}"
        gz = f"{plain}.gz"
        if os.path.exists(plain):
            path, opener = plain, open
        elif os.path.exists(gz):
            path, opener = gz, gzip.open
        else:
            break
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        except OSError:
            break
        yield path, opener
        if mtime < cutoff - timedelta(days=1):
            break
        n += 1


def parse_hits(hours):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    hits = []
    for path, opener in candidate_log_files(cutoff):
        try:
            mode = "rt" if opener is gzip.open else "r"
            with opener(path, mode, errors="replace") as f:
                for line in f:
                    m = LOG_LINE_RE.match(line)
                    if not m:
                        continue
                    try:
                        ts = datetime.strptime(m.group("time"), APACHE_TIME_FMT)
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue
                    hits.append((m.group("ip"), ts, m.group("request"), m.group("status")))
        except OSError:
            continue
    return hits


def build_report(hours, top_ips=5, include_private=False):
    hits = parse_hits(hours)
    if not include_private:
        hits = [h for h in hits if not is_private(h[0])]

    generated_at = datetime.now(timezone.utc).isoformat()
    if not hits:
        return {
            "generated_at": generated_at, "hours": hours,
            "total_hits": 0, "unique_ips": 0, "countries": [], "threats": [], "known": [],
        }

    ip_counts = Counter(h[0] for h in hits)
    threats = get_fail2ban_threats()

    known_counts = {ip: c for ip, c in ip_counts.items() if ip in KNOWN_IPS}
    external_counts = {ip: c for ip, c in ip_counts.items() if ip not in KNOWN_IPS}

    known = sorted(
        (
            {"ip": ip, "label": KNOWN_IPS[ip], "hits": count, "fail2ban": threats.get(ip)}
            for ip, count in known_counts.items()
        ),
        key=lambda d: -d["hits"],
    )

    by_country = defaultdict(Counter)
    for ip, count in external_counts.items():
        code, name = geolocate(ip)
        by_country[(code, name)][ip] = count

    total = sum(ip_counts.values())
    external_total = sum(external_counts.values()) or 1  # guard div-by-zero when all hits are known
    countries = []
    for (code, name), ip_counter in sorted(by_country.items(), key=lambda kv: -sum(kv[1].values())):
        country_total = sum(ip_counter.values())
        top = ip_counter.most_common(top_ips)
        entries = []
        for ip, c in top:
            org, location = rdap_lookup(ip)
            entries.append({
                "ip": ip, "hits": c, "fail2ban": threats.get(ip),
                "org": org, "location": location,
            })
        countries.append({
            "code": code,
            "name": name,
            "hits": country_total,
            "pct": round(100.0 * country_total / external_total, 1),
            "unique_ips": len(ip_counter),
            "top_ips": entries,
        })

    flagged = []
    for ip, count in external_counts.items():
        if ip not in threats:
            continue
        org, location = rdap_lookup(ip)
        flagged.append({
            "ip": ip, "hits": count, "fail2ban": threats[ip],
            "country": geolocate(ip)[1], "org": org, "location": location,
        })
    flagged.sort(key=lambda d: -d["hits"])

    return {
        "generated_at": generated_at,
        "hours": hours,
        "total_hits": total,
        "unique_ips": len(external_counts),
        "countries": countries,
        "threats": flagged,
        "known": known,
    }


def format_text(report, top_ips=5):
    lines = []
    hours = report["hours"]
    if report["total_hits"] == 0:
        lines.append(f"No hits found in the last {hours}h.")
        return "\n".join(lines)

    lines.append(f"CAP WxCOP access report — last {hours}h")
    lines.append(
        f"{report['total_hits']} total hits, {report['unique_ips']} unique external IPs "
        f"across {len(report['countries'])} countries"
    )
    if report["threats"]:
        lines.append(
            f"⚠ {len(report['threats'])} of those IPs are known threats per fail2ban"
        )
    lines.append("")

    if report["known"]:
        lines.append("=== known / internal ===")
        for k in report["known"]:
            lines.append(f"    {k['ip']:<16} {k['hits']:>6} hits  {k['label']}")
        lines.append("")

    if report["threats"]:
        lines.append("=== fail2ban-flagged hits ===")
        for t in report["threats"]:
            sources = ", ".join(t["fail2ban"])
            detail = t["org"] or t["country"]
            if t["location"]:
                detail += f" — {t['location']}"
            lines.append(f"    {t['ip']:<16} {t['hits']:>6} hits  [{detail}]  <- {sources}")
        lines.append("")

    for c in report["countries"]:
        lines.append(f"{c['name']} ({c['code']}): {c['hits']} hits ({c['pct']}%), {c['unique_ips']} IP(s)")
        for entry in c["top_ips"]:
            flag = f"  ⚠ [{', '.join(entry['fail2ban'])}]" if entry["fail2ban"] else ""
            detail = f"  {entry['org']}" if entry["org"] else ""
            if entry["location"]:
                detail += f" ({entry['location']})"
            lines.append(f"    {entry['ip']:<16} {entry['hits']:>6} hits{detail}{flag}")
        lines.append("")

    return "\n".join(lines)


def write_output(report, path):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(report, f, indent=2)
    os.chmod(tmp, 0o640)
    try:
        os.chown(tmp, -1, grp.getgrnam("www-data").gr_gid)
    except (KeyError, PermissionError):
        pass  # not run as root, or www-data group doesn't exist - file still gets written
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=24, help="lookback window (default 24)")
    ap.add_argument("--top-ips", type=int, default=5, help="top IPs shown per country (default 5)")
    ap.add_argument("--include-private", action="store_true",
                     help="include RFC1918/loopback IPs (internal health checks etc.)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a text report")
    ap.add_argument("-o", "--output", metavar="PATH",
                     help="write JSON report to PATH atomically (implies JSON content, "
                          "independent of --json for stdout)")
    args = ap.parse_args()

    report = build_report(args.hours, top_ips=args.top_ips, include_private=args.include_private)

    if args.output:
        write_output(report, args.output)

    if args.json:
        print(json.dumps(report, indent=2))
    elif not args.output:
        print(format_text(report, top_ips=args.top_ips))


if __name__ == "__main__":
    sys.exit(main())
