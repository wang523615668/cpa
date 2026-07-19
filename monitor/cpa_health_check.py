#!/usr/bin/env python3
"""CPA Grok pool health check + optional auto-replenish trigger.
Outputs JSON to stdout; non-zero exit if unhealthy.
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

CPA_AUTH = Path('/vol1/1000/openzl/cpa/auths')
GROK_REG = Path('/vol1/1000/openzl/grok-regkit')
SECRETS = Path('/vol1/1000/openzl/cpa/.secrets.env')
STATE = Path('/vol1/1000/openzl/cpa/monitor/health_state.json')
LOG_DIR = Path('/vol1/1000/openzl/cpa/logs')
MIN_HEALTHY = int(os.environ.get('CPA_MIN_HEALTHY', '30'))
TARGET_POOL = int(os.environ.get('CPA_TARGET_POOL', '100'))
CHAT_TIMEOUT = 45

def load_key() -> str:
    for line in SECRETS.read_text().splitlines():
        if line.startswith('CPA_API_KEY='):
            return line.split('=',1)[1].strip().strip('"').strip("'")
    raise SystemExit('missing CPA_API_KEY')

def list_auths():
    rows=[]
    now=datetime.now(timezone.utc)
    for p in sorted(CPA_AUTH.glob('xai-*.json')):
        try:
            d=json.loads(p.read_text())
        except Exception as e:
            rows.append({'file':p.name,'ok':False,'error':str(e)})
            continue
        exp=d.get('expired')
        has_refresh=bool(d.get('refresh_token'))
        has_access=bool(d.get('access_token'))
        disabled=bool(d.get('disabled'))
        rows.append({
            'file': p.name,
            'email': d.get('email'),
            'expired': exp,
            'has_refresh': has_refresh,
            'has_access': has_access,
            'disabled': disabled,
            'ok': has_refresh and has_access and not disabled,
        })
    return rows

def chat_probe(key: str) -> dict:
    payload=json.dumps({
        'model':'grok-4.5',
        'messages':[{'role':'user','content':'只回ok'}],
        'max_tokens':8,
        'temperature':0,
    }).encode()
    req=urllib.request.Request(
        'http://127.0.0.1:8317/v1/chat/completions',
        data=payload,
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'User-Agent': 'cpa-health-check/1.0',
        },
    )
    t0=time.time()
    try:
        with urllib.request.urlopen(req, timeout=CHAT_TIMEOUT) as r:
            body=r.read().decode()
            return {'ok': True, 'status': r.status, 'latency_s': round(time.time()-t0,2), 'body': body[:200]}
    except Exception as e:
        body=''
        code=getattr(e,'code',None)
        try: body=e.read().decode()[:300]
        except Exception: body=str(e)
        tag='error'
        if 'spending-limit' in body: tag='spending-limit'
        elif 'unknown provider' in body: tag='unknown-model'
        return {'ok': False, 'status': code, 'latency_s': round(time.time()-t0,2), 'tag': tag, 'body': body}

def recent_spending_errors(hours=6) -> int:
    if not LOG_DIR.exists():
        return 0
    cutoff=time.time()-hours*3600
    n=0
    for p in LOG_DIR.glob('error-*.log'):
        try:
            if p.stat().st_mtime < cutoff: continue
            if 'spending-limit' in p.read_text(errors='ignore'):
                n+=1
        except Exception:
            pass
    return n

def maybe_trigger_register(need: int) -> dict:
    """Emergency top-up: sequential one-account register + hard kill browsers.

    Never launches multi-account Chromium batches (OOMs 3.8GiB hosts).
    Uses register_one_then_kill.sh: each slot = hybrid register 1 + kill browsers.
    """
    if need <= 0:
        return {'triggered': False, 'reason': 'pool enough'}
    # avoid overlapping register (grok-regkit hybrid)
    try:
        out = subprocess.check_output(
            ['pgrep', '-af', 'run_hybrid|hybrid_register.py|grok_register_ttk.py'],
            text=True,
        )
        if out.strip():
            return {
                'triggered': False,
                'reason': 'register already running',
                'ps': out.strip()[:200],
            }
    except subprocess.CalledProcessError:
        pass
    # also skip if idle one-shot wrapper is mid-run
    try:
        out2 = subprocess.check_output(
            ['pgrep', '-af', 'register_one_then_kill.sh'], text=True
        )
        if out2.strip():
            return {
                'triggered': False,
                'reason': 'one-shot register already running',
                'ps': out2.strip()[:200],
            }
    except subprocess.CalledProcessError:
        pass

    env = os.environ.copy()
    env['DISPLAY'] = env.get('DISPLAY') or ':99'
    env['PYTHONUNBUFFERED'] = '1'
    env['CPA_TARGET_POOL'] = str(TARGET_POOL)
    # emergency: allow lower free-mem floor so hard floor recovery still works
    env['MIN_AVAIL_MB'] = os.environ.get('CPA_EMERGENCY_MIN_AVAIL_MB', '600')

    try:
        subprocess.check_call(
            ['xdpyinfo', '-display', env['DISPLAY']],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        subprocess.Popen(
            ['Xvfb', env['DISPLAY'], '-screen', '0', '1920x1080x24', '-ac'],
            start_new_session=True,
        )
        time.sleep(1)

    # Cap emergency batch: still one-at-a-time inside the script (max 3 slots)
    slots = max(1, min(int(need), 3))
    script = GROK_REG / 'scripts' / 'register_one_then_kill.sh'
    log = open('/tmp/grok_register_auto.log', 'a')
    log.write(
        f"\n=== auto one-shot register {datetime.now().isoformat()} "
        f"need={need} slots={slots} ===\n"
    )
    log.flush()
    cmd = ['/bin/bash', str(script), str(slots)]
    subprocess.Popen(
        cmd,
        cwd=str(GROK_REG),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return {
        'triggered': True,
        'mode': 'one_then_kill',
        'cmd': ' '.join(cmd),
        'extra': slots,
    }

def run_auth_scan() -> dict:
    """Classify invalid/exhausted via management API; quarantine spending; revive after 20h."""
    script = Path('/vol1/1000/openzl/cpa/monitor/cpa_auth_scan.py')
    if not script.exists():
        return {'ok': False, 'error': 'cpa_auth_scan.py missing'}
    try:
        # quarantine spending (rolling 24h — don't delete), try revive due ones
        out = subprocess.check_output(
            [sys.executable, str(script), '--quarantine', '--revive'],
            stderr=subprocess.STDOUT,
            timeout=60,
            text=True,
        )
        # read structured report
        rep_path = Path('/vol1/1000/openzl/cpa/monitor/auth_invalid_report.json')
        rep = json.loads(rep_path.read_text()) if rep_path.exists() else {}
        return {
            'ok': True,
            'summary': (out or '').strip().splitlines()[:6],
            'usable': rep.get('usable'),
            'total': rep.get('total'),
            'unavailable': rep.get('unavailable'),
            'by_tag': rep.get('by_tag'),
            'spending': len(rep.get('spending_list') or []),
            'actions': rep.get('actions') or {},
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)[:300]}


def main():
    key=load_key()
    # management scan first (may disable spending auths → file count of "ok" drops)
    scan = run_auth_scan()
    auths=list_auths()
    healthy=sum(1 for a in auths if a.get('ok'))
    # prefer usable from management scan when available
    usable = scan.get('usable')
    if isinstance(usable, int):
        effective_healthy = usable
    else:
        effective_healthy = healthy
    probe=chat_probe(key)
    spending=recent_spending_errors()
    # Gap vs target (informational). Do NOT launch Chromium just because we are
    # a few short of TARGET_POOL — that eats RAM on this 3.8GiB box.
    # Only auto-register when:
    #   1) effective usable < MIN_HEALTHY, or
    #   2) chat fails with spending-limit (quota exhaustion).
    gap_to_target = max(0, TARGET_POOL - effective_healthy)
    need = 0
    if effective_healthy < MIN_HEALTHY:
        need = max(need, MIN_HEALTHY - effective_healthy)
    if not probe.get('ok') and probe.get('tag') == 'spending-limit':
        need = max(need, 5)
        need = max(need, min(gap_to_target, 10))
    auto = os.environ.get('CPA_AUTO_REPLENISH', '1') == '1'
    trigger = {'triggered': False, 'gap_to_target': gap_to_target}
    if auto and need > 0:
        trigger = maybe_trigger_register(need)
        trigger['gap_to_target'] = gap_to_target
    report={
        'ts': datetime.now().isoformat(timespec='seconds'),
        'auth_total': len(auths),
        'auth_healthy_files': healthy,
        'auth_usable': effective_healthy,
        'auth_healthy': effective_healthy,  # back-compat for alerts
        'min_healthy': MIN_HEALTHY,
        'target_pool': TARGET_POOL,
        'need_more': need,
        'chat_probe': {k:v for k,v in probe.items() if k!='body'} | {'body_preview': (probe.get('body') or '')[:120]},
        'spending_errors_6h': spending,
        'auth_scan': scan,
        'auto_replenish': trigger,
        'quota_note': 'free-usage-exhausted = rolling 24h ~2M tokens; auto-revives after ~20h quarantine',
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # unhealthy exit for cron alerting
    if not probe.get('ok') or effective_healthy < MIN_HEALTHY:
        sys.exit(2)
    sys.exit(0)

if __name__ == '__main__':
    main()
