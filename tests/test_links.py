import unittest

from packfinder.links import classify_freebie, extract_urls, profile_telegram_links, soundcloud_profile_url, telegram_channel


class LinkTests(unittest.TestCase):
    def test_bio_web_profiles_and_preview_links_deduplicate(self):
        profile = {"description":"Packs: https://t.me/s/music_channel/123. Also t.me/music_channel", "web_profiles":[{"url":"https://telegram.me/other_music"}]}
        self.assertEqual(profile_telegram_links(profile),[("music_channel","https://t.me/music_channel"),("other_music","https://t.me/other_music")])

    def test_invites_login_and_lookalike_hosts_are_rejected(self):
        for link in ["https://t.me/+private","https://t.me/joinchat/abc","https://t.me/c/123/4","https://t.me/login","https://t.me.evil.test/music_channel","https://t.me@evil.test/music_channel","https://[broken"]:
            with self.subTest(link=link):
                self.assertIsNone(telegram_channel(link))

    def test_tg_resolve_deeplink(self):
        self.assertEqual(telegram_channel("tg://resolve?domain=Music_Channel"),"music_channel")
        self.assertIsNone(telegram_channel("tg://login?token=example"))

    def test_url_punctuation_is_removed(self):
        self.assertEqual(extract_urls("(https://example.org/pack.zip), t.me/music_channel."),["https://example.org/pack.zip","t.me/music_channel"])

    def test_profiles_only(self):
        self.assertEqual(soundcloud_profile_url("demo_artist"),"https://soundcloud.com/demo_artist")
        for url in ["http://soundcloud.com/demo","https://soundcloud.com/demo/track","https://soundcloud.com.evil/demo","https://evil@other.test/demo"]:
            with self.assertRaises(ValueError):
                soundcloud_profile_url(url)

    def test_free_music_offers(self):
        for caption in ["Free VST release from the creator","$0 sample pack","CC0 drum kit","Free presets for Vital"]:
            self.assertTrue(classify_freebie(caption)[0],caption)

    def test_price_and_license_wording_are_distinct(self):
        for caption in ["Royalty-free sound pack","Ad-free sample pack, $29","License-free VST","Premium drum kit $25","Free VST trial","This is not a free sound pack","Free shipping on this VST","Free sample pack with purchase"]:
            self.assertFalse(classify_freebie(caption)[0],caption)

    def test_unrelated_free_stuff_is_not_music(self):
        self.assertFalse(classify_freebie("Free wallpapers and avatars")[0])
        self.assertFalse(classify_freebie("Cracked VST free download")[0])


if __name__ == "__main__":
    unittest.main()
