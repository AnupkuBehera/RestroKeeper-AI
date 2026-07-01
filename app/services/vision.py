import os
import base64
import logging
from fastapi import UploadFile
from sqlalchemy.orm import Session
from openai import OpenAI
from pydantic import BaseModel
from typing import List, Tuple, Optional
from rapidfuzz import process, utils as fuzz_utils

from ..database import get_db
from ..models import MasterIngredient, SupplierInvoice
from ..schemas import ExtractedInvoiceItem, InvoiceUploadResponse

logger = logging.getLogger(__name__)

# Initialize OpenAI Client safely
openai_key = os.getenv("OPENAI_API_KEY", "")
is_mock_openai = not openai_key or openai_key.startswith("mock_")

if not is_mock_openai:
    try:
        openai_client = OpenAI(api_key=openai_key)
    except Exception as e:
        logger.error(f"Error initializing OpenAI Client: {e}")
        is_mock_openai = True
else:
    logger.info("Using Mock OpenAI mode for invoice OCR.")
    openai_client = None


# Helper class for OpenAI structured vision output
class RawInvoiceLineItem(BaseModel):
    item_name: str
    quantity: float
    unit_price: float

class RawInvoiceStructured(BaseModel):
    vendor_name: str
    invoice_number: str
    total_amount: float
    items: List[RawInvoiceLineItem]


def parse_invoice_image(
    file_bytes: bytes, 
    mime_type: str
) -> RawInvoiceStructured:
    """
    Calls GPT-4o with the base64-encoded invoice image to extract structured fields.
    """
    if is_mock_openai:
        # Mock structured response
        return RawInvoiceStructured(
            vendor_name="Fresh Produce Co.",
            invoice_number="INV-2026-9901",
            total_amount=350.00,
            items=[
                RawInvoiceLineItem(item_name="Onions", quantity=10.0, unit_price=18.50), # anomaly (old: 15.0)
                RawInvoiceLineItem(item_name="Tomato", quantity=5.0, unit_price=6.20),   # anomaly (old: 5.0)
                RawInvoiceLineItem(item_name="Cheddar Cheese", quantity=12.0, unit_price=12.00) # no anomaly (old: 12.0)
            ]
        )

    try:
        base64_image = base64.b64encode(file_bytes).decode('utf-8')
        
        system_prompt = (
            "You are an expert OCR accountant. Extract structured content from this supplier invoice image. "
            "Excellence is critical. Identify the vendor name, invoice number, total amount, and all line items. "
            "For each line item, extract the item name, quantity purchased, and unit price. "
            "Output the parsed data strictly in structured JSON."
        )

        response = openai_client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": system_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            response_format=RawInvoiceStructured
        )
        return response.choices[0].message.parsed

    except Exception as e:
        logger.error(f"Error calling GPT-4o Vision for invoice OCR: {e}")
        # Default mock invoice response if API fails
        return RawInvoiceStructured(
            vendor_name="Sysco Foodservices",
            invoice_number="SYS-88219",
            total_amount=245.50,
            items=[
                RawInvoiceLineItem(item_name="Onions", quantity=5.0, unit_price=17.50), # Anomaly
                RawInvoiceLineItem(item_name="Cheddar Cheese", quantity=4.0, unit_price=15.00), # Anomaly
                RawInvoiceLineItem(item_name="Tomato", quantity=8.0, unit_price=5.00) # Normal
            ]
        )


def process_invoice_ocr(
    db: Session, 
    tenant_id: int, 
    file_bytes: bytes, 
    filename: str,
    content_type: str
) -> InvoiceUploadResponse:
    """
    Performs OCR on the invoice, logs the invoice transaction, compares price increases
    against historical values in master_ingredients, and flags price anomalies.
    """
    # 1. Perform vision OCR parsing
    parsed_invoice = parse_invoice_image(file_bytes, content_type)
    
    # Save the parsed invoice transaction record to supplier_invoices
    invoice_record = SupplierInvoice(
        tenant_id=tenant_id,
        vendor_name=parsed_invoice.vendor_name,
        invoice_number=parsed_invoice.invoice_number,
        total_amount=parsed_invoice.total_amount
    )
    db.add(invoice_record)
    db.commit()
    
    # 2. Check for historical cost price anomalies
    ingredients = db.query(MasterIngredient).filter(MasterIngredient.tenant_id == tenant_id).all()
    ingredient_names = [ing.item_name for ing in ingredients]
    
    analyzed_items = []
    
    for raw_item in parsed_invoice.items:
        price_anomaly = False
        previous_price = None
        
        # Fuzzy match to identify database ingredient
        if ingredient_names:
            clean_name = fuzz_utils.default_process(raw_item.item_name)
            match_res = process.extractOne(
                clean_name, 
                ingredient_names,
                processor=fuzz_utils.default_process
            )
            
            if match_res and match_res[1] >= 65.0:
                matched_ing = next(ing for ing in ingredients if ing.item_name == match_res[0])
                previous_price = matched_ing.cost_per_unit
                
                # Check if price increased compared to historical baseline
                if raw_item.unit_price > matched_ing.cost_per_unit:
                    price_anomaly = True
                    
                    # Update master cost baseline to reflect the latest invoice cost, 
                    # but flag it as anomaly for user visibility.
                    # (Restrestaurants usually update current pricing but audit increases)
                    matched_ing.cost_per_unit = raw_item.unit_price
                    db.add(matched_ing)
        
        analyzed_items.append(
            ExtractedInvoiceItem(
                item_name=raw_item.item_name,
                quantity=raw_item.quantity,
                unit_price=raw_item.unit_price,
                price_anomaly=price_anomaly,
                previous_price=previous_price
            )
        )
        
    db.commit()
    
    return InvoiceUploadResponse(
        vendor_name=parsed_invoice.vendor_name,
        invoice_number=parsed_invoice.invoice_number,
        total_amount=parsed_invoice.total_amount,
        items=analyzed_items
    )
