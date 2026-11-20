"""Risk weights, unknown signals and read-only browser preference selection."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('environment_probe', Path(__file__).resolve().parents[1] / 'src/SafeCode.Core/Remote/environment_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class AssessmentTests(unittest.TestCase):
    def sample(self, **changes):
        sample = dict(timezone='America/New_York', languages=['en-US', 'en'], locale='en-US', offset=300,
                      simplifiedFonts=[], traditionalFonts=[], fontMeasurementAvailable=True, platform='Linux x86_64', userAgent='Linux Chrome')
        sample.update(changes)
        return sample

    def test_english_linux_has_only_emoji_baseline(self):
        result = probe.score_signals(self.sample(), 'workspace')
        self.assertEqual((result['riskScore'], result['level']), (4, 'low'))
        self.assertEqual(len(result['signals']), 6)
        self.assertEqual(sum(signal['weight'] for signal in result['signals']), 100)

    def test_mainland_signals_match_published_weights(self):
        sample = self.sample(timezone='Asia/Shanghai', languages=['zh-CN'], locale='zh-Hans-CN', offset=-480,
                             simplifiedFonts=['SimSun', 'Microsoft YaHei', 'SimHei'])
        result = probe.score_signals(sample, 'workspace')
        self.assertEqual(result['riskScore'], 94)
        self.assertEqual(result['level'], 'high')
        self.assertEqual([signal['contribution'] for signal in result['signals']], [30, 24, 20, 10, 6, 4])

    def test_traditional_signals_and_secondary_language(self):
        result = probe.score_signals(self.sample(timezone='Asia/Hong_Kong', languages=['zh-HK'], locale='zh-Hant-HK', offset=-480, traditionalFonts=['MingLiU']), 'workspace')
        self.assertEqual((result['riskScore'], result['level']), (55, 'medium'))
        secondary = probe.score_signals(self.sample(languages=['en-US', 'zh-CN']), 'workspace')
        self.assertEqual(secondary['signals'][1]['contribution'], 17)

    def test_missing_browser_signals_never_become_low_risk(self):
        for change in [dict(fontMeasurementAvailable=False), dict(timezone=''), dict(languages=[]), dict(locale=''), dict(offset=None)]:
            result = probe.score_signals(self.sample(**change), 'workspace')
            self.assertIsNone(result['riskScore'])
            self.assertEqual(result['level'], 'unavailable')

    def test_one_font_uses_javascript_rounding(self):
        result = probe.score_signals(self.sample(simplifiedFonts=['SimSun']), 'workspace')
        self.assertEqual(result['signals'][2]['contribution'], 17)

    def test_language_settings_are_read_without_rewriting_the_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / '.config/google-chrome/Profile 2'
            profile.mkdir(parents=True)
            state = profile.parent / 'Local State'
            state.write_text(json.dumps({'profile': {'last_used': 'Profile 2'}}))
            preferences = profile / 'Preferences'
            preferences.write_text(json.dumps({'intl': {'selected_languages': 'ko-KR,ko,en-US,en'}, 'unrelated': 'not-returned'}))
            before = preferences.read_bytes()
            user = SimpleNamespace(pw_dir=folder, pw_uid=-1)
            with patch.object(Path, 'iterdir', return_value=iter(())):
                _, languages = probe.browser_context(user)
            self.assertEqual(languages, 'ko-KR,ko,en-US,en')
            self.assertEqual(preferences.read_bytes(), before)

    def test_root_and_invalid_users_do_not_start_a_browser(self):
        with patch.object(probe.subprocess, 'Popen') as launch:
            self.assertIsNone(probe.assess('../root')['riskScore'])
            with patch.object(probe.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=0)):
                self.assertIsNone(probe.assess('root')['riskScore'])
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
