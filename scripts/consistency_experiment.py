#!/usr/bin/env python3
"""Experimento de consistencia: primario (5432) x replica (5433), assincrono x forte x falha.

Escreve em uma tabela propria (consistency_probe), nao toca job_history.
Variaveis: PGPASSWORD (padrao postgrespassword), N (padrao 50), PRIMARY_PORT, REPLICA_PORT.
"""
import datetime
import os
import statistics
import subprocess
import threading
import time

import psycopg2

PW = os.getenv("POSTGRES_PASSWORD", "postgrespassword")
N = int(os.getenv("N", "50"))
HOST = os.getenv("PGHOST", "localhost")
PP, RP = int(os.getenv("PRIMARY_PORT", "5432")), int(os.getenv("REPLICA_PORT", "5433"))
REPLICA_CT = os.getenv("REPLICA_CONTAINER", "hospeda_db_replica")
FAIL_TIMEOUT_S = 5


def conn(port, **kw):
    c = psycopg2.connect(host=HOST, port=port, user="postgres", password=PW,
                         dbname="hospedasync_db", connect_timeout=3, **kw)
    c.autocommit = True
    return c


def set_sync(p, on):
    cur = p.cursor()
    if on:
        cur.execute("ALTER SYSTEM SET synchronous_standby_names = '*'")
        cur.execute("ALTER SYSTEM SET synchronous_commit = 'remote_apply'")
    else:
        cur.execute("ALTER SYSTEM RESET synchronous_standby_names")
        cur.execute("ALTER SYSTEM RESET synchronous_commit")
    cur.execute("SELECT pg_reload_conf()")
    time.sleep(1.5)


def wait_streaming(p, timeout=60):
    cur = p.cursor()
    end = time.time() + timeout
    while time.time() < end:
        cur.execute("SELECT count(*) FROM pg_stat_replication WHERE state='streaming'")
        if cur.fetchone()[0]:
            return True
        time.sleep(1)
    return False


def run_round(p, r, n):
    pc, rc = p.cursor(), r.cursor()
    stale, wlat, lags = 0, [], []
    for i in range(n):
        t = time.perf_counter()
        pc.execute("INSERT INTO consistency_probe(payload) VALUES (%s) RETURNING id", (f"x{i}",))
        pid = pc.fetchone()[0]
        wlat.append((time.perf_counter() - t) * 1000)
        rc.execute("SELECT (SELECT count(*) FROM consistency_probe WHERE id=%s), "
                   "COALESCE(EXTRACT(EPOCH FROM (now()-pg_last_xact_replay_timestamp()))*1000,0)", (pid,))
        seen, lag = rc.fetchone()
        stale += 0 if seen else 1
        lags.append(float(lag))
    return {"n": n, "stale": stale, "stale_pct": 100.0 * stale / n,
            "lag_avg": statistics.mean(lags), "lag_max": max(lags),
            "w_avg": statistics.mean(wlat), "w_max": max(wlat),
            "w_p95": sorted(wlat)[int(0.95 * (n - 1))]}


def timed_write(port_conn_factory):
    c = port_conn_factory()
    cur = c.cursor()
    # statement_timeout nao interrompe a espera por replica sincrona; usa cancelamento do cliente.
    timer = threading.Timer(FAIL_TIMEOUT_S, c.cancel)
    t = time.perf_counter()
    timer.start()
    try:
        cur.execute("INSERT INTO consistency_probe(payload) VALUES ('falha')")
        outcome = "concluiu"
    except Exception as e:
        outcome = "BLOQUEOU ate o timeout de %ds (%s)" % (FAIL_TIMEOUT_S, type(e).__name__)
    timer.cancel()
    dt = time.perf_counter() - t
    if outcome == "concluiu" and dt >= FAIL_TIMEOUT_S * 0.9:
        outcome = ("BLOQUEOU %ds ate o cancelamento do cliente (commit ficou so no primario, "
                   "sem confirmacao da replica)" % FAIL_TIMEOUT_S)
    c.close()
    return outcome, dt


def main():
    p, r = conn(PP), conn(RP)
    p.cursor().execute("CREATE TABLE IF NOT EXISTS consistency_probe("
                       "id serial PRIMARY KEY, payload text, ts timestamptz DEFAULT now())")
    time.sleep(1)
    rc = r.cursor()
    rc.execute("SELECT pg_is_in_recovery()")
    assert rc.fetchone()[0], "replica nao esta em recovery"
    res = {}
    try:
        print(f"== Modo ASSINCRONO (N={N}) ==")
        set_sync(p, False)
        res["async"] = run_round(p, r, N)
        print(res["async"])
        print(f"== Modo FORTE (synchronous_commit=remote_apply, N={N}) ==")
        set_sync(p, True)
        res["strong"] = run_round(p, r, N)
        print(res["strong"])
        print("== Falha: replica parada ==")
        subprocess.run(["docker", "stop", REPLICA_CT], check=True, capture_output=True)
        time.sleep(2)
        res["fail_strong"] = timed_write(lambda: conn(PP))
        set_sync(p, False)
        res["fail_async"] = timed_write(lambda: conn(PP))
        print(res["fail_strong"], res["fail_async"])
    finally:
        try:
            set_sync(p, False)
        except Exception as e:
            print("AVISO: restaurar assincrono falhou:", e)
        subprocess.run(["docker", "start", REPLICA_CT], capture_output=True)
        ok = wait_streaming(conn(PP))
        print("replica de volta em streaming:", ok)
    return res


def fmt(res):
    a, s = res["async"], res["strong"]
    fs, fa = res["fail_strong"], res["fail_async"]
    rows = [
        ("Modo", "Leituras obsoletas", "Lag medio (ms)", "Lag max (ms)", "Escrita media (ms)", "Escrita p95 (ms)", "Escrita max (ms)"),
        ("Assincrono", f"{a['stale']}/{a['n']} ({a['stale_pct']:.0f}%)", f"{a['lag_avg']:.1f}", f"{a['lag_max']:.1f}",
         f"{a['w_avg']:.2f}", f"{a['w_p95']:.2f}", f"{a['w_max']:.2f}"),
        ("Forte (remote_apply)", f"{s['stale']}/{s['n']} ({s['stale_pct']:.0f}%)", f"{s['lag_avg']:.1f}", f"{s['lag_max']:.1f}",
         f"{s['w_avg']:.2f}", f"{s['w_p95']:.2f}", f"{s['w_max']:.2f}"),
    ]
    w = [max(len(r[i]) for r in rows) for i in range(7)]
    out = ["| " + " | ".join(c.ljust(w[i]) for i, c in enumerate(r)) + " |" for r in rows]
    out.insert(1, "|" + "|".join("-" * (x + 2) for x in w) + "|")
    t = "\n".join(out)
    cost = s["w_avg"] / a["w_avg"] if a["w_avg"] else float("nan")
    f = (f"\nReplica parada (timeout {FAIL_TIMEOUT_S}s por escrita):\n"
         f"  - modo forte    : {fs[0]} em {fs[1]:.2f}s\n"
         f"  - modo assincrono: {fa[0]} em {fa[1]:.3f}s\n"
         f"Custo da consistencia forte na escrita: {cost:.1f}x a latencia media assincrona.\n")
    return t, f


if __name__ == "__main__":
    res = main()
    table, fail = fmt(res)
    print("\n" + table + fail)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "felipe", "resultados_consistencia.md")
    path = os.path.normpath(path)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("# Resultados do experimento de consistencia\n\nGerado por `scripts/consistency_experiment.sh` (primario hospeda_db x replica hospeda_db_replica).\n")
        fh.write(f"\n## Execucao {datetime.datetime.now():%Y-%m-%d %H:%M:%S} (N={N})\n\n{table}\n{fail}")
    print("Resultados acrescentados em", path)
