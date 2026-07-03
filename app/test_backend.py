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
        # Clean up existing test ingredient to make the test idempotent
        from app.database import SessionLocal
        from app.models import MasterIngredient
        db = SessionLocal()
        try:
            db.query(MasterIngredient).filter(MasterIngredient.SKU_code == "SKU-AVO-99").delete()
            db.commit()
        finally:
            db.close()

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

    def test_auth_signup_and_login(self):
        """Test SaaS user registration and login JWT flow."""
        signup_payload = {
            "name": "Test Chef",
            "email": "chef_test@bistro.com",
            "password": "password123",
            "restaurant_name": "Test Bistro Cafe"
        }
        # Clear existing user if already exists from previous runs
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            old_user = db.query(User).filter(User.email == signup_payload["email"]).first()
            if old_user:
                db.delete(old_user)
                db.commit()
        finally:
            db.close()

        # Signup
        response = self.client.post("/api/auth/signup", json=signup_payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("access_token", data)
        self.assertEqual(data["token_type"], "bearer")
        token = data["access_token"]

        # Login
        login_payload = {
            "email": "chef_test@bistro.com",
            "password": "password123"
        }
        response = self.client.post("/api/auth/login", json=login_payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("access_token", response.json())

        # Test authenticated call
        headers = {"Authorization": f"Bearer {token}"}
        response = self.client.get("/api/ingredients", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.json()), 0)

    def test_menu_item_and_recipe_creation(self):
        """Test creating a menu item mapped to recipe ingredients."""
        # 1. Fetch ingredients to map
        ing_res = self.client.get("/api/ingredients", headers={"X-Tenant-ID": "1"})
        ingredients = ing_res.json()
        dal_id = next(ing["id"] for ing in ingredients if ing["item_name"] == "Dal")
        
        # 2. Create menu item with Dal requirement
        menu_payload = {
            "name": "Super Dal Tadka",
            "price": 8.99,
            "recipes": [
                {"ingredient_id": dal_id, "quantity_required": 0.3}
            ]
        }
        
        # Clean up existing menu item if exists
        from app.database import SessionLocal
        from app.models import MenuItem
        db = SessionLocal()
        try:
            db.query(MenuItem).filter(MenuItem.name == "Super Dal Tadka").delete()
            db.commit()
        finally:
            db.close()

        response = self.client.post("/api/menu", json=menu_payload, headers={"X-Tenant-ID": "1"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "Super Dal Tadka")
        self.assertEqual(len(data["recipes"]), 1)
        self.assertEqual(data["recipes"][0]["ingredient_id"], dal_id)
        self.assertEqual(data["recipes"][0]["quantity_required"], 0.3)

    def test_sale_deduction_fifo(self):
        """Test POS sales stock deduction with FIFO batching."""
        # 1. Fetch current ingredient list
        ing_res = self.client.get("/api/ingredients", headers={"X-Tenant-ID": "1"})
        ingredients = ing_res.json()
        atta_item = next(ing for ing in ingredients if ing["item_name"] == "Atta")
        atta_id = atta_item["id"]
        
        # Clear existing batches
        from app.database import SessionLocal
        from app.models import StockBatch, MenuItem
        db = SessionLocal()
        try:
            db.query(StockBatch).filter(StockBatch.master_ingredient_id == atta_id).delete()
            db.query(MenuItem).filter(MenuItem.name == "Bistro Roti").delete()
            db.commit()
        finally:
            db.close()

        # 2. Add two stock batches for Atta
        from datetime import datetime, timedelta
        exp1 = (datetime.now() + timedelta(days=2)).isoformat()
        exp2 = (datetime.now() + timedelta(days=10)).isoformat()
        
        batch1_payload = {
            "master_ingredient_id": atta_id,
            "quantity": 5.0,
            "expiry_date": exp1
        }
        batch2_payload = {
            "master_ingredient_id": atta_id,
            "quantity": 10.0,
            "expiry_date": exp2
        }
        self.client.post("/api/inventory/batches", json=batch1_payload, headers={"X-Tenant-ID": "1"})
        self.client.post("/api/inventory/batches", json=batch2_payload, headers={"X-Tenant-ID": "1"})

        # Verify batches are listed
        batches_res = self.client.get("/api/inventory/batches", headers={"X-Tenant-ID": "1"})
        self.assertEqual(batches_res.status_code, 200)
        atta_batches = [b for b in batches_res.json() if b["master_ingredient_id"] == atta_id]
        self.assertEqual(len(atta_batches), 2)

        # 3. Create a menu item using Atta
        menu_payload = {
            "name": "Bistro Roti",
            "price": 1.50,
            "recipes": [
                {"ingredient_id": atta_id, "quantity_required": 1.0}
            ]
        }
        menu_res = self.client.post("/api/menu", json=menu_payload, headers={"X-Tenant-ID": "1"})
        menu_item_id = menu_res.json()["id"]

        # Get old stock level
        old_stock = self.client.get("/api/ingredients", headers={"X-Tenant-ID": "1"}).json()
        atta_old_stock = next(ing["current_stock"] for ing in old_stock if ing["id"] == atta_id)

        # 4. Log a sale of 6 Bistro Roti (requires 6.0 units of Atta)
        # This should fully consume batch 1 (5.0 units) and take 1.0 unit from batch 2, leaving 9.0 units in batch 2
        sale_payload = {
            "menu_item_id": menu_item_id,
            "quantity_sold": 6
        }
        sale_res = self.client.post("/api/sales", json=sale_payload, headers={"X-Tenant-ID": "1"})
        self.assertEqual(sale_res.status_code, 200)

        # 5. Verify batch 1 is deleted and batch 2 quantity is 9.0
        batches_res = self.client.get("/api/inventory/batches", headers={"X-Tenant-ID": "1"})
        atta_batches_after = [b for b in batches_res.json() if b["master_ingredient_id"] == atta_id]
        self.assertEqual(len(atta_batches_after), 1)
        self.assertEqual(atta_batches_after[0]["quantity"], 9.0)

        # Verify master stock decreased by 6.0
        new_stock_res = self.client.get("/api/ingredients", headers={"X-Tenant-ID": "1"})
        atta_new_stock = next(ing["current_stock"] for ing in new_stock_res.json() if ing["id"] == atta_id)
        self.assertAlmostEqual(atta_new_stock, atta_old_stock - 6.0)

    def test_wastage_logging(self):
        """Test logging wastage for an ingredient."""
        ing_res = self.client.get("/api/ingredients", headers={"X-Tenant-ID": "1"})
        ingredients = ing_res.json()
        dal_item = next(ing for ing in ingredients if ing["item_name"] == "Dal")
        dal_id = dal_item["id"]
        old_stock = dal_item["current_stock"]

        wastage_payload = {
            "master_ingredient_id": dal_id,
            "quantity_wasted": 1.5,
            "reason": "Water damage"
        }
        response = self.client.post("/api/inventory/wastage", json=wastage_payload, headers={"X-Tenant-ID": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["quantity_wasted"], 1.5)
        
        # Verify master stock level decreased
        new_ing_res = self.client.get("/api/ingredients", headers={"X-Tenant-ID": "1"})
        dal_new_stock = next(ing["current_stock"] for ing in new_ing_res.json() if ing["id"] == dal_id)
        self.assertAlmostEqual(dal_new_stock, max(0.0, old_stock - 1.5))


if __name__ == "__main__":
    unittest.main()
