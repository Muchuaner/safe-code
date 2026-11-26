#!/bin/bash
# Stage the desktop-api tools, desktop_gateway.py and codex_bridge.zip together.
set -euo pipefail
dir=$(cd "$(dirname "$0")" && pwd)

stage_program() {
    local target=$1
    for file in desktop_gateway.py server.py management.py claude_usage.py pricing.py configure_cloudcli.py host_control_mcp.py device_bridge.py device_service.py prompt_review.py codex_bridge.zip; do
        [[ -f "$dir/$file" ]] || { printf 'Missing SafeCode gateway component: %s\n' "$file" >&2; exit 1; }
    done
    install -d -m 755 "$target"
    install -m 644 "$dir"/{desktop_gateway,server,management,claude_usage,pricing,prompt_review}.py "$target/"
    install -m 644 "$dir"/{configure_cloudcli,host_control_mcp,device_bridge,device_service}.py "$target/"
    python3 - "$dir/codex_bridge.zip" "$target" <<'PY'
import hashlib,sys,zipfile
from pathlib import Path
p=Path(sys.argv[1])
assert hashlib.sha256(p.read_bytes()).hexdigest()=='3d6c8d98604e7301bd92046b00ead3b08eddefa157e1c5661d2c9599aefe58c3'
with zipfile.ZipFile(p) as z:z.extractall(Path(sys.argv[2])/'components')
PY
}

write_unit() {
    local unit=$1
    cat > "$unit" <<UNIT
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
}

if [[ ${1:-} == --prepare-repair ]]; then
    [[ $# == 6 ]]
    user=$2
    app=$3
    state=$4
    [[ "$user" =~ ^[a-z_][a-z0-9_-]*$ && "$user" != root ]]
    stage_program "$5"
    write_unit "$6"
    exit
fi

user=${1:?usage: install.sh linux-user bind-ip [port] [public-ip]}
bind=${2:?specify the listener IP address}
port=${3:-45143}
public_ip=${4:-}
home=$(getent passwd "$user" | cut -d: -f6)
[[ -n "$home" && "$user" =~ ^[a-z_][a-z0-9_-]*$ && "$user" != root ]]
app=/opt/safe-code/desktop-api
state="$home/.local/state/safe-code-desktop-api"
if [[ ${SAFECODE_INITIAL_API_INSTALL:-} == 1 ]]; then
    runuser -u "$user" -- test ! -e "$state/public-enabled" || {
        printf '檢測到已啟用過的公網網關配置，請在 VPS 恢復原服務。\n' >&2
        exit 1
    }
fi
stage_program "$app"
runuser -u "$user" -- python3 - "$state" "$bind" "$port" "$public_ip" "${SAFECODE_INITIAL_API_INSTALL:-}" <<'PY'
import json,sys,os,ipaddress
from pathlib import Path
state,bind,port,public_ip,initial=sys.argv[1:]
assert 1024<=int(port)<=65535
os.umask(0o077)
directory=Path(state)
directory.mkdir(parents=True,exist_ok=True)
(directory/'keys').mkdir(exist_ok=True)
p=directory/'config.new'
config_path=directory/'config.json'
config=json.loads(config_path.read_text()) if config_path.exists() else {}
if initial == '1' and config:
    saved_port = config.get('port')
    if config.get('bind') != bind or type(saved_port) is not int or not 1024 <= saved_port <= 65535:
        raise ValueError('檢測到保留的監聽配置，請在 VPS 恢復原服務。')
    if saved_port != int(port) and config.get('autoPort') != saved_port:
        raise ValueError('檢測到保留的監聽配置，請在 VPS 恢復原服務。')
    port = saved_port
config.update(bind=str(ipaddress.ip_address(bind)),port=int(port))
if initial == '1':
    config['autoPort'] = int(port)
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
if ! /usr/bin/python3 -B "$app/device_service.py" "$user" "$app" "$state"; then
    printf '私有裝置服務尚未就緒，繼續安裝普通 API。請在網關設定中執行故障修復後簽發桌面接入 Key。\n' >&2
fi
write_unit /etc/systemd/system/safe-code-desktop-api.service
systemctl daemon-reload
systemctl enable safe-code-desktop-api.service
systemctl restart safe-code-desktop-api.service
