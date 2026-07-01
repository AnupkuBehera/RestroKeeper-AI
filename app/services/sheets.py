import os
import json
import logging
from typing import List, Dict, Optional
from datetime import datetime
from sqlalchemy.orm import Session

import gspread
from google.oauth2.service_account import Credentials

from ..models import MasterIngredient, Tenant

logger = logging.getLogger(__name__)

# Load config
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
CREDENTIALS_JSON = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "")

is_sheets_mock = not SHEET_ID or not CREDENTIALS_JSON or CREDENTIALS_JSON.startswith("mock_")
client = None
spreadsheet = None

# Initialize Google Sheets connection
if not is_sheets_mock:
    try:
        # Load credentials either from raw inline JSON string or file path
        if CREDENTIALS_JSON.strip().startswith("{"):
            creds_info = json.loads(CREDENTIALS_JSON)
            creds = Credentials.from_service_account_info(
                creds_info, 
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        else:
            creds = Credentials.from_service_account_file(
                CREDENTIALS_JSON, 
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
        logger.info(f"Google Sheets Sync: Successfully connected to Spreadsheet ID: {SHEET_ID}")
    except Exception as e:
        logger.error(f"Google Sheets connection failed. Running in Sandbox Mock Mode: {e}")
        is_sheets_mock = True
else:
    logger.info("Google Sheets: Credentials not configured. Running in Sandbox Mock Mode.")


def get_worksheet(title: str, headers: List[str]) -> Optional[gspread.Worksheet]:
    """Retrieves a worksheet, creating it with headers if it does not exist."""
    if is_sheets_mock or not spreadsheet:
        return None
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        try:
            ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers) + 2)
            ws.append_row(headers)
            logger.info(f"Google Sheets: Created worksheet '{title}' with headers.")
            return ws
        except Exception as e:
            logger.error(f"Failed to create worksheet '{title}': {e}")
            return None


def sync_ingredients_from_sheet(db: Session, tenant_id: int = 1):
    """
    Two-way synchronization:
    1. If the Google Sheet 'Ingredients' tab has records, pulls and updates/inserts local SQL cache.
    2. If the 'Ingredients' tab is empty, uploads the current database ingredients to populate the sheet.
    """
    headers = ["SKU_code", "item_name", "current_stock", "safety_par_level", "unit_type", "cost_per_unit", "vendor_name"]
    
    if is_sheets_mock:
        logger.info("[Sheets Sandbox] Simulating sync ingredients from sheet to local DB.")
        return {"status": "mock", "message": "Spreadsheet simulation: Synced ingredients cached."}

    try:
        ws = get_worksheet("Ingredients", headers)
        if not ws:
            return

        records = ws.get_all_records()
        
        # Scenario A: Sheet has records -> Sync from Sheet to SQL database
        if records:
            logger.info(f"Google Sheets Sync: Pulling {len(records)} items from sheet to local DB.")
            for row in records:
                sku = str(row.get("SKU_code", "")).strip()
                if not sku:
                    continue
                
                # Fetch ingredient from DB
                db_ing = db.query(MasterIngredient).filter(
                    MasterIngredient.tenant_id == tenant_id,
                    MasterIngredient.SKU_code == sku
                ).first()
                
                # Map columns safely
                name = str(row.get("item_name", "Unknown"))
                stock = float(row.get("current_stock", 0.0) or 0.0)
                par = float(row.get("safety_par_level", 0.0) or 0.0)
                unit = str(row.get("unit_type", "units"))
                cost = float(row.get("cost_per_unit", 0.0) or 0.0)
                vendor = str(row.get("vendor_name", "Generic Supplier"))
                
                if db_ing:
                    # Update local database columns with sheet values
                    db_ing.item_name = name
                    db_ing.current_stock = stock
                    db_ing.safety_par_level = par
                    db_ing.unit_type = unit
                    db_ing.cost_per_unit = cost
                    db_ing.vendor_name = vendor
                    db.add(db_ing)
                else:
                    # Create new local database ingredient from sheet row
                    new_ing = MasterIngredient(
                        tenant_id=tenant_id,
                        SKU_code=sku,
                        item_name=name,
                        current_stock=stock,
                        safety_par_level=par,
                        unit_type=unit,
                        cost_per_unit=cost,
                        vendor_name=vendor
                    )
                    db.add(new_ing)
            db.commit()
            logger.info("Google Sheets Sync: DB updated from Sheet.")
            
        # Scenario B: Sheet is empty -> Upload SQL ingredients to Google Sheet
        else:
            logger.info("Google Sheets Sync: Spreadsheet is empty. Populating sheet with DB ingredients.")
            ingredients = db.query(MasterIngredient).filter(MasterIngredient.tenant_id == tenant_id).all()
            for ing in ingredients:
                ws.append_row([
                    ing.SKU_code,
                    ing.item_name,
                    ing.current_stock,
                    ing.safety_par_level,
                    ing.unit_type,
                    ing.cost_per_unit,
                    ing.vendor_name
                ])
            logger.info(f"Google Sheets Sync: Populated sheet with {len(ingredients)} items.")
            
    except Exception as e:
        logger.error(f"Failed to sync ingredients from sheets: {e}")


def update_sheet_ingredient_stock(sku: str, stock: float):
    """Updates the stock count cell for the matching SKU code in Google Sheet."""
    headers = ["SKU_code", "item_name", "current_stock", "safety_par_level", "unit_type", "cost_per_unit", "vendor_name"]
    
    if is_sheets_mock:
        logger.info(f"[Sheets Sandbox] Simulating update sheet: Stock for SKU {sku} set to {stock}.")
        return

    try:
        ws = get_worksheet("Ingredients", headers)
        if not ws:
            return

        # Find matching cell under SKU_code column (Column A)
        cell_list = ws.findall(sku, in_column=1)
        if cell_list:
            row_idx = cell_list[0].row
            # Current stock is Column C (index 3)
            ws.update_cell(row_idx, 3, stock)
            logger.info(f"Google Sheets Sync: Updated stock cell for {sku} to {stock} in row {row_idx}.")
        else:
            logger.warning(f"Google Sheets Sync: SKU {sku} not found to update stock.")
    except Exception as e:
        logger.error(f"Failed to update stock cell in sheet: {e}")


def add_sheet_ingredient(sku: str, name: str, stock: float, par: float, unit: str, cost: float, vendor: str):
    """Appends a new ingredient row in Google Sheet."""
    headers = ["SKU_code", "item_name", "current_stock", "safety_par_level", "unit_type", "cost_per_unit", "vendor_name"]
    
    if is_sheets_mock:
        logger.info(f"[Sheets Sandbox] Simulating register ingredient: SKU {sku} ({name}) added.")
        return

    try:
        ws = get_worksheet("Ingredients", headers)
        if not ws:
            return
            
        # Verify SKU does not already exist
        cell_list = ws.findall(sku, in_column=1)
        if not cell_list:
            ws.append_row([sku, name, stock, par, unit, cost, vendor])
            logger.info(f"Google Sheets Sync: Appended new ingredient row for SKU {sku} ({name}).")
    except Exception as e:
        logger.error(f"Failed to add ingredient row to sheet: {e}")


def log_po_to_sheet(vendor_name: str, items: List[Dict]):
    """Logs approved purchase order items as rows in 'Purchase_Orders' worksheet tab."""
    headers = ["vendor_name", "item_name", "quantity", "unit", "total_cost", "logged_at"]
    
    if is_sheets_mock:
        logger.info(f"[Sheets Sandbox] Simulating log PO: {len(items)} items saved to spreadsheet.")
        return

    try:
        ws = get_worksheet("Purchase_Orders", headers)
        if not ws:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for item in items:
            ws.append_row([
                vendor_name,
                item.get("item_name", "Unknown"),
                item.get("recommended_order_qty", 0.0),
                item.get("unit", "units"),
                item.get("total_cost", 0.0),
                timestamp
            ])
        logger.info(f"Google Sheets Sync: Logged {len(items)} PO lines to 'Purchase_Orders'.")
    except Exception as e:
        logger.error(f"Failed to log PO details to sheet: {e}")


def log_audit_to_sheet(invoice_number: str, vendor_name: str, items: List[Dict]):
    """Logs scanned invoice item checks and cost anomalies to 'Price_Audits' worksheet tab."""
    headers = ["invoice_number", "vendor_name", "item_name", "quantity", "unit_price", "price_anomaly", "checked_at"]
    
    if is_sheets_mock:
        logger.info(f"[Sheets Sandbox] Simulating log price audit: {len(items)} invoice items logged.")
        return

    try:
        ws = get_worksheet("Price_Audits", headers)
        if not ws:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for item in items:
            ws.append_row([
                invoice_number,
                vendor_name,
                item.get("item_name", "Unknown"),
                item.get("quantity", 0.0),
                item.get("unit_price", 0.0),
                "YES" if item.get("price_anomaly") else "NO",
                timestamp
            ])
        logger.info(f"Google Sheets Sync: Logged {len(items)} audit checks to 'Price_Audits'.")
    except Exception as e:
        logger.error(f"Failed to log audit details to sheet: {e}")
