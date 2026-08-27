import unittest
import tempfile

from ownerplot.domain import Listing, SellerType
from ownerplot.policy import enforce_contact_policy
from ownerplot.processing import classify_seller, deduplicate, normalize_phone, validate_locality
from ownerplot.query import parse_query
from ownerplot.collectors import _area, _phone, _price
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

    def test_phone(self):
        self.assertEqual(normalize_phone("+91 98765 43210"), "+919876543210")

    def test_owner(self):
        item = classify_seller(listing())
        self.assertEqual(item.seller_type, SellerType.VERIFIED_OWNER)

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


if __name__ == "__main__":
    unittest.main()
