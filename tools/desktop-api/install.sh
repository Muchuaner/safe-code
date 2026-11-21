#!/bin/bash
# Stage the desktop-api tools, desktop_gateway.py and codex_bridge.zip together.
set -euo pipefail
user=${1:?usage: install.sh linux-user bind-ip [port] [public-ip]}
bind=${2:?specify the listener IP address}
port=${3:-45143}
public_ip=${4:-}
home=$(getent passwd "$user" | cut -d: -f6)
[[ -n "$home" && "$user" =~ ^[a-z_][a-z0-9_-]*$ && "$user" != root && -x "$home/.local/bin/claude" ]]
app=/opt/safe-code/desktop-api
state="$home/.local/state/safe-code-desktop-api"
node=/opt/safe-code/node/bin/node
[[ -x "$node" ]] || node=$(command -v node)
[[ $("$node" -p 'Number(process.versions.node.split(".")[0]) >= 20') == true ]]
dir=$(cd "$(dirname "$0")" && pwd)
if [[ ${SAFECODE_INITIAL_API_INSTALL:-} == 1 ]]; then
    runuser -u "$user" -- test ! -e "$state/public-enabled" || {
        printf '检测到已启用过的公网网关配置，请在 VPS 恢复原服务。\n' >&2
        exit 1
    }
fi
for file in desktop_gateway.py server.py management.py claude_usage.py pricing.py configure_cloudcli.py host_control_mcp.py device_bridge.py codex_bridge.zip; do
    [[ -f "$dir/$file" ]] || { printf 'Missing SafeCode gateway component: %s\n' "$file" >&2; exit 1; }
done
install -d -m 755 "$app"
install -m 644 "$dir"/{desktop_gateway,server,management,claude_usage,pricing}.py "$app/"
install -m 644 "$dir"/{configure_cloudcli,host_control_mcp,device_bridge}.py "$app/"
python3 - "$dir/codex_bridge.zip" "$app" <<'PY'
import hashlib,sys,zipfile
from pathlib import Path
p=Path(sys.argv[1])
assert hashlib.sha256(p.read_bytes()).hexdigest()=='3d6c8d98604e7301bd92046b00ead3b08eddefa157e1c5661d2c9599aefe58c3'
with zipfile.ZipFile(p) as z:z.extractall(Path(sys.argv[2])/'components')
PY
runuser -u "$user" -- python3 - "$state" "$bind" "$port" "$node" "$app" "$public_ip" "${SAFECODE_INITIAL_API_INSTALL:-}" <<'PY'
import json,sys,os,ipaddress
from pathlib import Path
state,bind,port,node,app,public_ip,initial=sys.argv[1:]
assert 1024<=int(port)<=65535
os.umask(0o077)
directory=Path(state)
directory.mkdir(parents=True,exist_ok=True)
(directory/'keys').mkdir(exist_ok=True)
p=directory/'config.new'
config_path=directory/'config.json'
config=json.loads(config_path.read_text()) if config_path.exists() else {}
if initial == '1' and config and (config.get('bind'), config.get('port')) != (bind, int(port)):
    raise ValueError('检测到保留的监听配置，请在 VPS 恢复原服务。')
config.update(bind=str(ipaddress.ip_address(bind)),port=int(port),node=node,library=app+'/components')
if public_ip:
    address=ipaddress.ip_address(public_ip)
    if not address.is_global or address.is_multicast:
        raise ValueError('public-ip must be a public unicast IP address')
    config['publicIp']=str(address)
p.write_text(json.dumps(config))
p.replace(config_path)
PY
if [[ ! -e "$state/key.pem" && ! -e "$state/cert.pem" ]]; then
    runuser -u "$user" -- openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 730 -subj '/CN=SafeCode Desktop API' -keyout "$state/key.pem" -out "$state/cert.pem"
fi
cat > /etc/systemd/system/safe-code-desktop-api.service <<UNIT
[Unit]
Description=Safe Code API
After=network-online.target
Wants=network-online.target
[Service]
User=$user
WorkingDirectory=$app
ExecStart=/usr/bin/python3 -B $app/server.py --state $state serve
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable safe-code-desktop-api.service
systemctl restart safe-code-desktop-api.service
