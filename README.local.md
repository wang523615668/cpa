# CPA (CLIProxyAPI) local deploy

Path: `/vol1/1000/openzl/cpa`

## Status
- Binary: `/vol1/1000/openzl/cpa/cli-proxy-api` (v7.2.86 linux aarch64, upgraded from 7.2.74 on 2026-07-18; backup `cli-proxy-api.bak.7.2.74`). Changelog highlights: xAI image URL fix, reasoning token support, xAI compact request base URL, xAI tool schema improvements, oauth model alias display names.
- Config: `/vol1/1000/openzl/cpa/config.yaml`
- Port: `8317`
- LAN reverse proxy: `http://192.168.3.226:5689` -> `8317`
- Auth dir: `/vol1/1000/openzl/cpa/auths`
- Secrets: `/vol1/1000/openzl/cpa/.secrets.env`

## Use
```bash
# models
curl http://127.0.0.1:8317/v1/models \
  -H "Authorization: Bearer $CPA_API_KEY"

# chat
curl http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer $CPA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.5","messages":[{"role":"user","content":"hi"}],"max_tokens":64}'
```

## Mint more Grok OAuth auths
Preferred factory: **grok-regkit** (legacy `grok_reg` removed 2026-07-16; backup under `/vol1/1000/openzl/backups/`).
```bash
export DISPLAY=:99
# one-shot hybrid + CPA hotload
bash /vol1/1000/openzl/grok-regkit/scripts/register_one_then_kill.sh 1 --force
# or direct:
cd /vol1/1000/openzl/grok-regkit
systemd-run --user --collect --unit=grok-regkit-one \
  --setenv=DISPLAY=:99 --working-directory=$PWD \
  .venv/bin/python -u run_hybrid_n.py 1
```

## Notes
- Sub2API Grok path failed because accounts lacked refresh_token.
- Sub2API was removed after CPA chat worked.
- Proxy for upstream: `http://127.0.0.1:7890`


## Public
- https://cpa.523615668.xyz
- https://cp.523615668.xyz
- Built-in panel only: https://cpa.523615668.xyz/management.html (or LAN `http://192.168.3.226:5689/management.html`)
- CPA-Manager removed 2026-07-16 (container/volumes/image + tunnel hostnames cpa-manager/cm deleted)
