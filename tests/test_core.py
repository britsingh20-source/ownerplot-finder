import unittest
import tempfile
import json
import os
import asyncio
from unittest.mock import patch

from cryptography.fernet import Fernet

from ownerplot.authorized_contacts import capture_contact, credit_status, enrich_authorized_contacts, parse_capture_command
from ownerplot.domain import Listing, SellerType
from ownerplot.policy import enforce_contact_policy
from ownerplot.processing import analyze_seller_history, classify_seller, correlate_public_contacts, deduplicate, normalize_phone, validate_locality
from ownerplot.query import parse_query
from ownerplot.collectors import _area, _enrich_youtube_descriptions, _original_post_date, _phone, _price, _xml_entries, _youtube_video_id
from datetime import datetime, timezone
from ownerplot.cache import WatchStore


def listing(**changes):
    values = dict(source="test", source_id="1", url="https://example.test/1", title="Owner residential plot in Kalapatti", description="Direct owner no brokerage", locality="Kalapatti", property_type="plot", price=4_200_000, area_sqft=1089, phone="9876543210", phone_public=True)
    values.update(changes)
    return Listing(**values)


class CoreTests(unittest.TestCase):
    def test_query(self):
        query = parse_query("Search owner plots in Kalapatti under 60 lakhs")
        self.assertEqual(query.locality, "Kalapatti")
        self.assertEqual(query.max_price, 6_000_000)
        self.assertEqual(query.max_age_days, 90)

    def test_phone(self):
        self.assertEqual(normalize_phone("+91 98765 43210"), "+919876543210")

    def test_owner(self):
        item = classify_seller(listing())
        self.assertEqual(item.seller_type, SellerType.PROBABLE_OWNER)
        self.assertEqual(item.contact_verification, "probable_owner_call_to_confirm")

    def test_owner_requires_two_source_property_match(self):
        first=listing(source="one")
        second=listing(source="two",source_id="2")
        analyze_seller_history([first,second])
        self.assertEqual(classify_seller(first).seller_type,SellerType.VERIFIED_OWNER)

    def test_high_volume_owner_claim_is_broker(self):
        item=listing(description="Direct owner no brokerage 808 items listed")
        self.assertEqual(classify_seller(item).seller_type,SellerType.BROKER)

    def test_original_post_date(self):
        now=datetime(2026,8,27,tzinfo=timezone.utc)
        value,confidence,_=_original_post_date("Posted 3 days ago",now=now)
        self.assertEqual(value.date().isoformat(),"2026-08-24")
        self.assertEqual(confidence,80)

    def test_rss_and_sitemap_parsing(self):
        rss="""<rss><channel><item><title>Kalapatti plot</title><link>https://example.test/p/1</link><pubDate>Wed, 26 Aug 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
        sitemap="""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.test/p/2</loc><lastmod>2026-08-25</lastmod></url></urlset>"""
        self.assertEqual(_xml_entries(rss)[0]["title"],"Kalapatti plot")
        self.assertEqual(_xml_entries(sitemap)[0]["published"],"2026-08-25")

    def test_private_phone_removed(self):
        item = enforce_contact_policy(listing(phone_public=False))
        self.assertIsNone(item.phone)

    def test_locality(self):
        self.assertEqual(validate_locality(listing(), "Kalapatti"), 100)
        self.assertEqual(validate_locality(listing(), "Sulur"), 0)

    def test_duplicate(self):
        self.assertEqual(len(deduplicate([listing(), listing(source_id="2")])), 1)

    def test_public_page_fields(self):
        text = "Direct owner plot 2.5 cents price Rs 42 lakhs call +91 98765 43210"
        self.assertEqual(_phone(text), "9876543210")
        self.assertEqual(_price(text), 4_200_000)
        self.assertAlmostEqual(_area(text), 1089)

    def test_malformed_area_is_ignored(self):
        self.assertIsNone(_area("plot area , sqft"))

    def test_watch_baseline_and_new_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WatchStore(f"{directory}/test.sqlite3")
            query = parse_query("plots in Kalapatti")
            store.add(123, 456, query)
            self.assertEqual(store.list(123)[0][2].locality, "Kalapatti")
            self.assertEqual(store.new_fingerprints(123, "Kalapatti", ["one"]), ["one"])
            self.assertEqual(store.new_fingerprints(123, "Kalapatti", ["one", "two"]), ["two"])

    def test_authorized_capture_is_encrypted_and_reused(self):
        state={}
        url="https://www.99acres.com/property-in-kalapatti-coimbatore-123?tracking=test"
        with patch.dict(os.environ,{"CONTACT_STORE_KEY":Fernet.generate_key().decode()},clear=False):
            first=capture_contact(state,url,"98765 43210")
            second=capture_contact(state,url,"98765 43210")
            self.assertTrue(first["new_credit"])
            self.assertFalse(second["new_credit"])
            self.assertNotIn("9876543210",json.dumps(state))
            item=listing(source="99acres.com",url=url,phone=None,phone_public=False,seller_claim="owner")
            enriched=enrich_authorized_contacts([item],state)[0]
            self.assertEqual(enriched.phone,"+919876543210")
            self.assertEqual(enriched.contact_verification,"authorized_captured_owner")
            self.assertIn("99acres: 1/25",credit_status(state))

    def test_hidden_owner_listing_is_prioritized_for_reveal(self):
        item=listing(source="magicbricks.com",url="https://www.magicbricks.com/propertyDetails/example-123",phone=None,phone_public=False,seller_claim="posted by owner",date_confidence=80,locality_confidence=100)
        enriched=enrich_authorized_contacts([item],{})[0]
        self.assertTrue(enriched.reveal_required)
        self.assertGreaterEqual(enriched.reveal_priority,90)

    def test_capture_command_rejects_unapproved_portal(self):
        with self.assertRaises(ValueError):
            parse_capture_command("/capture https://example.com/property/1 9876543210")

    def test_youtube_id_and_public_description_contact(self):
        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {"items":[{"id":"abc123","snippet":{"title":"Kalapatti owner plot","description":"Direct owner call +91 98765 43210","publishedAt":"2026-08-26T10:00:00Z","channelTitle":"Owner"}}]}
        class Client:
            async def get(self,*args,**kwargs): return Response()
        item=listing(source="youtube.com",url="https://www.youtube.com/watch?v=abc123",phone=None,phone_public=False)
        self.assertEqual(_youtube_video_id(item.url),"abc123")
        with patch.dict(os.environ,{"YOUTUBE_API_KEY":"test-key"},clear=False):
            enriched=asyncio.run(_enrich_youtube_descriptions([item],Client()))[0]
        self.assertEqual(enriched.phone,"9876543210")
        self.assertTrue(enriched.phone_public)

    def test_public_contact_cross_source_match(self):
        portal=listing(source="magicbricks.com",url="https://www.magicbricks.com/propertyDetails/example",phone=None,phone_public=False,seller_claim="contact owner",title="East plot near SVB Tech Park",description="Kalapatti 1500 sqft price 44.5 lakh",price=4_450_000,area_sqft=1500)
        social=listing(source="youtube.com",url="https://youtube.com/watch?v=public",title="SVB Tech Park east plot",description="Kalapatti 1500 sqft price 44.5 lakh",price=4_450_000,area_sqft=1500)
        social.seller_type=SellerType.UNKNOWN
        result=correlate_public_contacts([portal,social])
        self.assertEqual(result[0].phone,"+919876543210")
        self.assertEqual(result[0].seller_type,SellerType.VERIFIED_OWNER)

    def test_public_contact_requires_area_and_price_match(self):
        portal=listing(source="99acres.com",url="https://www.99acres.com/property/1",phone=None,phone_public=False,seller_claim="owner",price=4_450_000,area_sqft=1500)
        unrelated=listing(source="facebook.com",url="https://facebook.com/post/1",price=5_500_000,area_sqft=2400)
        correlate_public_contacts([portal,unrelated])
        self.assertIsNone(portal.phone)


if __name__ == "__main__":
    unittest.main()
