import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('desktop_gateway_target_guard', ROOT/'src/SafeCode.Core/Remote/desktop_gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class DeviceTargetGuardTests(unittest.TestCase):
    def backend(self, device=None):
        value = gateway.CloudCliBackend.__new__(gateway.CloudCliBackend)
        value.config = {'hostControl': None}
        value.device = device
        return value

    def test_unbound_conversation_names_vps_and_blocks_local_desktop_substitution(self):
        text = self.backend().host_instruction('/tmp/vps-project', None)
        self.assertIn('VPS working account', text)
        self.assertIn("not on the API user's computer", text)
        self.assertIn('stop and ask them to authorize client-device control', text)
        self.assertIn('Never substitute a similarly named VPS directory', text)

    def test_bound_mac_conversation_names_the_actual_client(self):
        device = {'platform': 'macos', 'hostname': 'Example Mac', 'command': 'ssh -F fixture mac-test'}
        text = self.backend(device).host_instruction('/tmp/device-project', '/Users/example/Desktop')
        self.assertIn('Example Mac', text)
        self.assertIn('macOS', text)
        self.assertIn('/Users/example/Desktop', text)
        self.assertIn('ssh -F fixture mac-test', text)

    def test_safecode_client_cannot_fall_back_to_an_unbound_relay(self):
        access = (ROOT/'src/Shared/DesktopKeyAccess.cs').read_text()
        server = (ROOT/'tools/desktop-api/server.py').read_text()
        self.assertIn('"X-SafeCode-Client","safecode-desktop"', access)
        self.assertIn("self.headers.get('X-SafeCode-Client') == 'safecode-desktop'", server)
        self.assertIn('旧接入未绑定本机设备', access)
        capability = access.index('action="capability"')
        self.assertNotIn('optional:true', access[capability:capability+120])
        self.assertIn('action="capability"', access)

    def test_mac_has_complete_pairing_and_cleanup_lifecycle(self):
        program = (ROOT/'src/SafeCode.MacBackend/Program.cs').read_text()
        tunnel = (ROOT/'src/SafeCode.MacBackend/MacDeviceTunnel.cs').read_text()
        host = (ROOT/'src/SafeCode.MacBackend/MacHostControl.cs').read_text()
        for operation in ('desktopKey.authorizeDevice', 'desktopKey.cancelDevice', 'desktopKey.deviceStatus'):
            self.assertIn('case "'+operation+'"', program)
        self.assertIn('checkDevice:true', program)
        self.assertIn('await StopKeyDeviceAsync();', program)
        self.assertIn('device.Alias != "mac-" + device.DeviceId', tunnel)
        self.assertIn('X-SafeCode-Platform: macos', tunnel)
        self.assertIn('await frpc.WaitForExitAsync()', tunnel)
        self.assertIn('EnableDeviceAsync(string publicKey', host)
        self.assertIn('Authorize(publicKey)', host)
        self.assertIn('RevokeLocal();if(session!=null)', host)

    def test_windows_disable_also_waits_for_frpc(self):
        tunnel = (ROOT/'src/SafeCode.Desktop/WindowsDeviceTunnel.cs').read_text()
        program = (ROOT/'src/SafeCode.Desktop/MainWindow.cs').read_text()
        self.assertIn('frpc.Kill(entireProcessTree: true)', tunnel)
        self.assertIn('await frpc.WaitForExitAsync()', tunnel)
        self.assertIn('finally{await StopKeyDeviceAsync();}', program)
        self.assertIn('finally{if(hostControl?.Enabled==true)await hostControl.DisableAsync();}', program)

    def test_mac_consent_copy_does_not_claim_windows_admin_deployment(self):
        source = (ROOT/'web/native.js').read_text()
        self.assertIn('只会为当前 macOS 账户添加一把专用 SSH 公钥', source)
        self.assertIn('正在为系统远程登录添加专用公钥，并建立 FRP 通道', source)

    def test_mac_build_pins_official_frp_and_preserves_license(self):
        source = (ROOT/'tools/build_macos.py').read_text()
        self.assertIn("frp_version='0.65.0'", source)
        self.assertIn('bce3badaf40cf5d3811c9eae593f3b24833bf7cb13b8b7c3f9453e94af45ded1', source)
        self.assertIn("name in ('frpc','LICENSE')", source)
        self.assertIn("(frp_payload/'frpc').chmod(0o755)", source)


if __name__ == '__main__':
    unittest.main()
