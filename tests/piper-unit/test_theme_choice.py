"""N1: the theme is a Settings › Interface choice - policy "theme" (dark by default),
validated against the four choices, applied at once, and sent to the GUI in gui_flags."""
import unittest

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import guard
import settings


class ThemeChoiceTest(unittest.TestCase):
    def field(self):
        return next(f for f in settings.section("interface").fields if f.key == "theme")

    def test_interface_offers_four_themes_dark_by_default(self):
        f = self.field()
        self.assertEqual(f.type, "enum")
        self.assertEqual([c[0] for c in f.choices], ["dark", "light", "auto", "indigo"])
        self.assertEqual(f.default, "dark")
        self.assertEqual(f.apply, "now")
        self.assertEqual(guard.DEFAULT_POLICY["theme"], "dark")
        self.assertNotIn("acme", repr(f.choices).lower(), "no company brand in the GUI: the theme is Indigo")

    def test_only_a_known_theme_is_accepted(self):
        f = self.field()
        for v in ("dark", "light", "auto", "indigo"):
            self.assertEqual(settings._validate(f, v), v)
        for v in ("solarized", "acme", "", None, 1):
            with self.assertRaises(ValueError):
                settings._validate(f, v)


if __name__ == "__main__":
    unittest.main()
