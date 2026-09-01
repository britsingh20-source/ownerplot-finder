import unittest

from ownerplot.contact_resolver import _score
from ownerplot.domain import Listing


class ContactResolverScoreTests(unittest.TestCase):
    def test_strong_property_crosspost_scores_high(self):
        target = Listing(
            source="magicbricks.com",
            source_id="mb-1",
            url="https://www.magicbricks.com/propertyDetails/example",
            title="Owner: Arun D Plot For Sale in Kalapatti",
            description="2600 sqft residential plot 65 X 40 east facing near the airport",
            locality="Kalapatti",
            property_type="plot",
            price=10_200_000,
            area_sqft=2600,
            seller_claim="Arun D",
        )
        text = "Arun D direct owner Kalapatti 2600 sqft plot 65 X 40 near airport. Contact 9876543210."
        score, evidence = _score(target, text, "https://example.com/crosspost")
        self.assertGreaterEqual(score, 75)
        self.assertIn("same plot area", evidence)
        self.assertIn("same dimensions", evidence)

    def test_same_name_without_property_evidence_stays_low(self):
        target = Listing(
            source="magicbricks.com",
            source_id="mb-2",
            url="https://www.magicbricks.com/propertyDetails/example2",
            title="Owner: Bhoopathy Plot For Sale in Kalapatti",
            description="2085 sqft east facing residential plot",
            locality="Kalapatti",
            property_type="plot",
            price=12_500_000,
            area_sqft=2085,
            seller_claim="Bhoopathy",
        )
        score, evidence = _score(target, "Bhoopathy runs a business in Coimbatore. Phone 9876543210.", "https://example.com/profile")
        self.assertLess(score, 75)
        self.assertNotIn("same plot area", evidence)


if __name__ == "__main__":
    unittest.main()
