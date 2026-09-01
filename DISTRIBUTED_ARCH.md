# CAP WxCOP Distributed Architecture

## Server Roles

| Host  | IP (LAN)      | IP (External)   | Role |
|-------|---------------|-----------------|------|
| r815  | 192.168.0.200 | 209.248.90.253  | LDM relay + Apache web serving |
| data1 | 192.168.0.61  | none            | LDM full ingest + NFS export |
| data2 | 192.168.0.60  | none            | PostgreSQL 18/PostGIS 3.6 + ingest + map generation |

> **2026-09-01 correction:** data2 has no external interface — it was carrying a stale `209.248.90.252`
> entry here. Verified via `ip -4 addr show scope global` on data2: only `192.168.0.60` is present.
> r815 is the only host with a routed external IP. All three hosts run `ufw` (default deny incoming);
> data2 additionally has no public-facing NIC at all, so its inbound exposure is LAN-only regardless
> of firewall rules. See ufw rule sets in the LDM/systemd infra notes for what's opened where.

## Data Flow
```
Unidata IDD upstream
  -> r815 LDM relay
      -> data1 LDM full ingest (pqact writes to /data/LDM on 6TB drive)
          -> /data/LDM NFS exported read-only to r815 and data2
              -> data2 ingest scripts read /LDM via NFS, write to PostgreSQL
              -> data2 batch_generate_maps.py reads DB, rsyncs PNGs to r815
              -> r815 Apache serves web app + static maps from PostgreSQL on data2
```

## Storage Layout

### data1
- sda: Ubuntu OS (100G LV) + /data/LDM tree (4.88T LV) on 6TB Seagate Enterprise
- sdb: /data/models (2.93T) + /data/radar (2.53T) on second 6TB drive
- /data/LDM/models and /data/LDM/radar bind-mounted from sdb spindle

### data2
- sda: Ubuntu OS (100G LV) on 6TB drive, remainder available for working storage
- sdb: PostgreSQL data directory (/data/postgresql, 5.46T) dedicated spindle

## Key Configuration

- **DB host:** 192.168.0.60 (data2), port 5432, database avwx_data
- **NFS server:** data1 exports /data/LDM with crossmnt
- **Map generation:** data2 runs batch_generate_maps.py, rsyncs to r815 web root
- **Load result:** r815 load average reduced from 24-70 to ~5

## Infrastructure Files

- `infrastructure/ldm/ldmd_r815_relay.conf` - r815 LDM relay configuration
- `infrastructure/ldm/ldmd_data1.conf` - data1 LDM full ingest configuration
- `infrastructure/ldm/pqact_*.conf` - pqact processing configurations (run on data1)
- `infrastructure/systemd/ldm-r815.service` - LDM systemd service for r815
- `infrastructure/systemd/ldm-data1.service` - LDM systemd service for data1
