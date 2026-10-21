import unittest
from engine.search.agent import _protection_reason


class ProtectionReasonTests(unittest.TestCase):
    def test_explicit_providers_are_named(self):
        for marker, name in [('KillBot','KillBot'),('Cloudflare','Cloudflare'),
                             ('Turnstile','Cloudflare Turnstile'),
                             ('SmartCaptcha','Yandex SmartCaptcha'),
                             ('reCAPTCHA','Google reCAPTCHA'),('hCaptcha','hCaptcha'),
                             ('DDoS-Guard','DDoS-Guard'),('Qrator','Qrator')]:
            with self.subTest(marker=marker):
                reason=_protection_reason('Verify you are human '+marker, status=403)
                self.assertIn(name,reason)
                self.assertIn('HTTP 403',reason)

    def test_generic_challenge_does_not_invent_provider(self):
        reason=_protection_reason('Я не робот')
        self.assertIn('тип защиты не определён',reason)
        self.assertNotIn('KillBot',reason)

    def test_plain_http_denial_is_not_attributed_to_bots(self):
        reason=_protection_reason('Forbidden',status=403)
        self.assertIn('причина не установлена',reason)
        self.assertIn('защита от ботов не подтверждена',reason)

    def test_title_marker_can_identify_protection(self):
        self.assertIn('KillBot',_protection_reason('Denied','KillBot check'))
