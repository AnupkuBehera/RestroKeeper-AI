import os
import sys
import unittest
from fastapi.testclient import TestClient

# Ensure root directory is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app

class TestRestoKeeperAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_root_endpoint(self):
        """Test health status check."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "online")
        self.assertEqual(data["service"], "RestoKeeper AI Backend")

    def test_get_ingredients(self):
        """Test fetching the seeded list of master ingredients with tenant header."""
        response = self.client.get("/api/ingredients", headers={"X-Tenant-ID": "1"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        first_item = data[0]
        self.assertIn("item_name", first_item)
        self.assertIn("current_stock", first_item)
        self.assertIn("safety_par_level", first_item)
        self.assertIn("cost_per_unit", first_item)

    def test_get_ingredients_invalid_tenant(self):
        """Test that invalid tenant headers fail validation."""
        response = self.client.get("/api/ingredients", headers={"X-Tenant-ID": "abc"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("X-Tenant-ID header must be an integer", response.json()["detail"])

    def test_voice_upload(self):
        """Test speech upload parsing with simulated files and headers."""
        files = {
            "file": ("simulated_audio.wav", b"Onions, 9 bags", "audio/wav")
        }
        response = self.client.post("/api/inventory/voice-upload", files=files, headers={"X-Tenant-ID": "1"})
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertIn("transcript", json_data)
        self.assertIn("extracted_items", json_data)
        self.assertIn("updated_ingredients", json_data)

    def test_invoice_ocr(self):
        """Test invoice uploading OCR, pricing audits and headers."""
        files = {
            "file": ("sysco_invoice.jpg", b"mock_image_bytes_here", "image/jpeg")
        }
        response = self.client.post("/api/procurement/upload-invoice", files=files, headers={"X-Tenant-ID": "1"})
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertIn("vendor_name", json_data)
        self.assertIn("invoice_number", json_data)
        self.assertIn("items", json_data)
        first_item = json_data["items"][0]
        self.assertIn("item_name", first_item)
        self.assertIn("price_anomaly", first_item)

    def test_draft_orders(self):
        """Test AI-generated procurement order routing and calculations."""
        response = self.client.get("/api/procurement/draft-orders", headers={"X-Tenant-ID": "1"})
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertIn("draft_orders", json_data)
        self.assertIn("total_procurement_cost", json_data)
        self.assertIn("critical_stock_alerts", json_data)

    def test_send_purchase_order(self):
        """Test sending/approving drafted purchase orders."""
        payload = {
            "vendor_name": "Fresh Produce Co.",
            "items": [
                {"item_name": "Onions", "recommended_order_qty": 10.0, "unit": "bags", "total_cost": 150.0}
            ],
            "recipient_email": "orders@freshproduce.com"
        }
        response = self.client.post("/api/procurement/send-po", json=payload, headers={"X-Tenant-ID": "1"})
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertEqual(json_data["status"], "success")
        self.assertIn("PO simulated", json_data["message"])

    def test_create_ingredient(self):
        """Test creating a custom ingredient."""
        payload = {
            "SKU_code": "SKU-AVO-99",
            "item_name": "Avocado",
            "current_stock": 0.0,
            "safety_par_level": 5.0,
            "unit_type": "boxes",
            "cost_per_unit": 18.50,
            "vendor_name": "Fresh Produce Co."
        }
        response = self.client.post("/api/ingredients", json=payload, headers={"X-Tenant-ID": "1"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["item_name"], "Avocado")
        self.assertEqual(data["SKU_code"], "SKU-AVO-99")

    def test_clear_all_ingredient_stocks(self):
        """Test resetting all current stock values to 0.0."""
        response = self.client.post("/api/ingredients/clear-stocks", headers={"X-Tenant-ID": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

    def test_reset_endpoint(self):
        """Test reset database counts driver."""
        response = self.client.post("/api/ingredients/reset", headers={"X-Tenant-ID": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")


if __name__ == "__main__":
    unittest.main()
