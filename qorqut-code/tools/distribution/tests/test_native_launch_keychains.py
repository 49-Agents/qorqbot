"""The private home links the user's macOS keychains so agent CLIs can reach their sign-in.

    python3.12 -m unittest discover -s tools/distribution/tests -v
"""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_launch import link_user_keychains  # noqa: E402


class KeychainLinkTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.user, self.home = base / 'user', base / 'home'
        (self.user / 'Library/Keychains').mkdir(parents=True)
        self.home.mkdir()

    def test_links_only_the_keychains_folder(self):
        link_user_keychains(self.home, self.user)
        link = self.home / 'Library/Keychains'
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (self.user / 'Library/Keychains').resolve())
        self.assertEqual(sorted(p.name for p in (self.home / 'Library').iterdir()), ['Keychains'])

    def test_repeat_is_harmless_and_existing_entries_are_kept(self):
        link_user_keychains(self.home, self.user)
        link_user_keychains(self.home, self.user)
        other = Path(tempfile.mkdtemp())
        (other / 'Library/Keychains').mkdir(parents=True)
        (other / 'Library/Keychains/mine').write_text('x')
        link_user_keychains(other, self.user)
        self.assertFalse((other / 'Library/Keychains').is_symlink())
        self.assertTrue((other / 'Library/Keychains/mine').exists())

    def test_nothing_without_user_keychains_or_when_home_is_the_user_home(self):
        empty = Path(tempfile.mkdtemp())
        link_user_keychains(self.home, empty)
        self.assertFalse((self.home / 'Library/Keychains').exists())
        link_user_keychains(self.user, self.user)
        self.assertFalse((self.user / 'Library/Keychains').is_symlink())


if __name__ == '__main__':
    unittest.main()
