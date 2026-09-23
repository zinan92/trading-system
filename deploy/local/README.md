# Local always-on deployment (macOS launchd + Cloudflare Tunnel)

This is how `trade.park-ai-intel.com` actually runs: two launchd agents on a Mac
and one `cloudflared` tunnel that publishes only the desk (8790). The dashboard
(8765) is never exposed directly; the desk forwards the paths GridMind needs and
puts its passcode gate in front of them.

```
Internet ──cloudflared──▶ 127.0.0.1:8790 trading-desk ──▶ 127.0.0.1:8765 dashboard
                                    │                          │
                                    └── desk.db                └── outputs/ (receipts, ledgers)
```

## 1. Render the templates

Every template uses three placeholders. Substitute them and drop the results in
`~/Library/LaunchAgents/`:

```bash
REPO="$HOME/work/trading-system"      # your clone
USER_NAME="$(whoami)"
for f in deploy/local/*.plist.template; do
  out="$HOME/Library/LaunchAgents/$(basename "${f%.template}")"
  sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" -e "s#__USER__#$USER_NAME#g" "$f" > "$out"
done
mkdir -p "$HOME/Library/Logs/TradingPlatform"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.trading-platform.dashboard.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.trading-platform.desk.plist"
```

Both agents read `<repo>/.env` through the `scripts/run-*.sh` wrappers, so the
plist files never carry secrets or personal paths.

## 2. Publish the desk

```bash
cloudflared tunnel login
cloudflared tunnel create trading-desk
# note the tunnel UUID, then:
sed -e "s#<TUNNEL_ID>#<uuid>#" -e "s#<HOSTNAME>#trade.example.com#" -e "s#__HOME__#$HOME#" \
    deploy/local/cloudflared.yml.example > ~/.cloudflared/trading-desk.yml
cloudflared tunnel route dns trading-desk trade.example.com
cloudflared --config ~/.cloudflared/trading-desk.yml tunnel run   # or install it as a service
```

Before you open the hostname, create the desk passcode file (mode 0600):

```bash
mkdir -p ~/park-data/trading-desk
umask 077; printf '%s\n' 'choose-a-long-passcode' > ~/park-data/trading-desk/remote-passcode
```

Requests that arrive through the tunnel must hold a session derived from that
passcode. Requests from the Mac itself never need it.

## Gotcha: where the boot receipt lives

`make gate` writes `outputs/release_gates/paper_predeploy_current.json` under
the repo, and the dashboard reads it from `$TRADING_ORCHESTRATOR_OUTPUT_ROOT`.
With the default root they are the same directory. If you point the root
elsewhere, copy the receipt there after every `make gate`, or the dashboard
exits with `paper_predeploy_source_sha_mismatch`.

## 3. Check

```bash
curl -s http://127.0.0.1:8765/api/park-paper/read-model | head -c 200
curl -s http://127.0.0.1:8790/api/health
tail -f ~/Library/Logs/TradingPlatform/*.log
```
