"""Check the actual desktop payload before either installer is assembled."""
from pathlib import Path


def verify_gateway_bundle(pages, core):
    root = Path(__file__).resolve().parents[1]
    for name in ('gateway.html', 'gateway.js', 'multi-gateway.js', 'multi-gateway.css'):
        if (pages / name).read_bytes() != (root / 'web' / name).read_bytes():
            raise ValueError(f'网关界面组件与发布源码不一致：{name}')
    html = (pages / 'gateway.html').read_text()
    if not all(name in html for name in ('multi-gateway.js', 'multi-gateway.css', 'gateway-version')):
        raise ValueError('网关总览缺少版本或绑定入口。')
    bundle = core.read_bytes()
    for name in ('gateway_directory.py', 'server.py'):
        if (root / 'tools' / 'desktop-api' / name).read_bytes() not in bundle:
            raise ValueError(f'网关后端组件未正确内嵌：{name}')
    script = (pages / 'multi-gateway.js').read_text()
    for entry in ('生成绑定码', '绑定设备', 'multi.create', 'multi.claim'):
        if entry not in script:
            raise ValueError(f'网关绑定功能不完整：{entry}')
