import os
import math
import logging
from sqlalchemy.orm import Session
from openai import OpenAI
from pydantic import BaseModel
from typing import List, Dict

from ..database import get_db
from ..models import MasterIngredient
from ..schemas import DraftOrderItem, VendorDraftOrder, DraftOrdersResponse

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
    logger.info("Using Mock OpenAI mode for smart procurement.")
    openai_client = None


# Helper schemas for GPT structured response
class GPTPurchaseOrderItem(BaseModel):
    item_name: str
    recommended_order_qty: float
    unit: str
    reasoning: str

class GPTPurchaseOrderVendorGroup(BaseModel):
    vendor_name: str
    items: List[GPTPurchaseOrderItem]

class GPTPurchaseOrderList(BaseModel):
    vendor_groups: List[GPTPurchaseOrderVendorGroup]


def calculate_local_draft_orders(ingredients: List[MasterIngredient]) -> DraftOrdersResponse:
    """
    Locally computes recommended order quantities, forecasting, and rounding
    to standard packaging units, grouping items by vendor.
    Used as the mock fallback and logic verifier.
    """
    vendor_groups_map: Dict[str, List[DraftOrderItem]] = {}
    critical_alerts = 0
    total_procurement_cost = 0.0

    # Standard packaging units mapping for rounding logic
    package_units = {
        "bags": 5.0,     # e.g., Onions sold in bags of 5
        "blocks": 2.0,   # e.g., Cheddar Cheese sold in cases of 2
        "kg": 10.0,      # e.g., Tomato/Flour sold in bags of 10kg
        "boxes": 1.0,     # e.g., Beef Patties sold in boxes of 1
        "cases": 1.0
    }

    for ing in ingredients:
        # Mock forecast: upcoming week's consumption is roughly 1.4x of par level
        forecasted_need = round(ing.safety_par_level * 1.4, 1)
        
        # Check stock alerts (below par)
        is_critical = ing.current_stock < ing.safety_par_level
        if is_critical:
            critical_alerts += 1
            
        # Deficit calculation
        deficit = max(0.0, ing.safety_par_level - ing.current_stock)
        
        # If stock is below par OR won't survive upcoming week
        if is_critical or (ing.current_stock < forecasted_need):
            # Target quantity to order = forecasted_need + safety_par_level - current_stock
            raw_needed = forecasted_need + ing.safety_par_level - ing.current_stock
            raw_needed = max(0.0, raw_needed)
            
            if raw_needed <= 0:
                continue
                
            # Round up to match supplier packaging units
            pkg_size = package_units.get(ing.unit_type.lower(), 1.0)
            recommended_qty = math.ceil(raw_needed / pkg_size) * pkg_size
            
            # Compute costs
            total_cost = round(recommended_qty * ing.cost_per_unit, 2)
            total_procurement_cost += total_cost
            
            item_draft = DraftOrderItem(
                item_name=ing.item_name,
                current_stock=ing.current_stock,
                safety_par_level=ing.safety_par_level,
                forecasted_demand=forecasted_need,
                recommended_order_qty=recommended_qty,
                unit=ing.unit_type,
                cost_per_unit=ing.cost_per_unit,
                total_cost=total_cost
            )
            
            vendor = ing.vendor_name or "Generic Supplier"
            if vendor not in vendor_groups_map:
                vendor_groups_map[vendor] = []
            vendor_groups_map[vendor].append(item_draft)

    # Format the draft orders grouped by vendor
    draft_orders = []
    for vendor, items in vendor_groups_map.items():
        total_vendor_cost = sum(item.total_cost for item in items)
        draft_orders.append(
            VendorDraftOrder(
                vendor_name=vendor,
                items=items,
                total_order_cost=round(total_vendor_cost, 2)
            )
        )

    # Analytics metrics
    total_waste_saved_estimate = round(critical_alerts * 45.20, 2) # mock estimation of waste saved
    pending_pos_count = len(draft_orders)

    return DraftOrdersResponse(
        draft_orders=draft_orders,
        total_procurement_cost=round(total_procurement_cost, 2),
        critical_stock_alerts=critical_alerts,
        pending_purchase_orders_count=pending_pos_count,
        total_waste_saved_estimate=total_waste_saved_estimate
    )


def generate_smart_procurement_drafts(
    db: Session, 
    tenant_id: int
) -> DraftOrdersResponse:
    """
    Aggregates database metrics and calls GPT-4o to intelligently draft
    purchase orders rounded to packaging specifications. Falls back to local math.
    """
    ingredients = db.query(MasterIngredient).filter(MasterIngredient.tenant_id == tenant_id).all()
    
    if not ingredients:
        return DraftOrdersResponse(
            draft_orders=[],
            total_procurement_cost=0.0,
            critical_stock_alerts=0,
            pending_purchase_orders_count=0,
            total_waste_saved_estimate=0.0
        )

    # Local computation is extremely robust. We always do local math first
    # to derive the exact values, then we can use GPT-4o to polish or reason 
    # about packaging rules if live API key is present.
    local_drafts = calculate_local_draft_orders(ingredients)
    
    if is_mock_openai:
        return local_drafts

    try:
        # Construct LLM prompt detailing items, current stocks, par levels, 
        # forecasted demand, and packaging sizes.
        prompt_items = []
        for item in local_drafts.draft_orders:
            for ing in item.items:
                prompt_items.append({
                    "item_name": ing.item_name,
                    "vendor": item.vendor_name,
                    "current_stock": ing.current_stock,
                    "safety_par_level": ing.safety_par_level,
                    "forecasted_demand": ing.forecasted_demand,
                    "unit": ing.unit,
                    "cost_per_unit": ing.cost_per_unit
                })

        system_prompt = (
            "You are a strategic restaurant procurement manager. Review the inventory stocks, "
            "safety par levels, upcoming forecasted demands, and external forecasting indicators.\n"
            "External Forecasting Indicators:\n"
            "- Upcoming Restaurant covers: Friday: 250 bookings, Saturday: 320 bookings.\n"
            "- Seasonal trend: Summer grill menu active (burgers, salads, onions usage +20%).\n"
            "- Weather forecast: Sunny and 28°C (drives high patio covers and salad prep).\n"
            "Group items by Vendor. Calculate recommended purchase orders.\n"
            "Round quantities up to standard supplier packaging units:\n"
            "- Onions: packages of 5 bags\n"
            "- Cheese: boxes of 2 blocks\n"
            "- Tomato: boxes of 10 kg\n"
            "- Beef/Meat: boxes of 1 unit\n"
            "Return the structured grouped orders."
        )

        response = openai_client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Compute procurement for ingredients: {prompt_items}"}
            ],
            response_format=GPTPurchaseOrderList
        )
        
        gpt_data = response.choices[0].message.parsed
        
        # Merge GPT recommendations with our database metrics to return a complete schema
        enhanced_drafts = []
        enhanced_total_cost = 0.0
        
        for gpt_vendor in gpt_data.vendor_groups:
            vendor_items = []
            vendor_total_cost = 0.0
            
            for gpt_item in gpt_vendor.items:
                # Find the matched database ingredient
                db_ing = next(
                    (ing for ing in ingredients if ing.item_name.lower() == gpt_item.item_name.lower()), 
                    None
                )
                if not db_ing:
                    continue
                    
                # Re-calculate forecast and costs based on GPT recommendation
                forecasted_need = round(db_ing.safety_par_level * 1.4, 1)
                cost = round(gpt_item.recommended_order_qty * db_ing.cost_per_unit, 2)
                vendor_total_cost += cost
                
                vendor_items.append(
                    DraftOrderItem(
                        item_name=db_ing.item_name,
                        current_stock=db_ing.current_stock,
                        safety_par_level=db_ing.safety_par_level,
                        forecasted_demand=forecasted_need,
                        recommended_order_qty=gpt_item.recommended_order_qty,
                        unit=db_ing.unit_type,
                        cost_per_unit=db_ing.cost_per_unit,
                        total_cost=cost
                    )
                )
                
            if vendor_items:
                enhanced_total_cost += vendor_total_cost
                enhanced_drafts.append(
                    VendorDraftOrder(
                        vendor_name=gpt_vendor.vendor_name,
                        items=vendor_items,
                        total_order_cost=round(vendor_total_cost, 2)
                    )
                )
                
        if enhanced_drafts:
            return DraftOrdersResponse(
                draft_orders=enhanced_drafts,
                total_procurement_cost=round(enhanced_total_cost, 2),
                critical_stock_alerts=local_drafts.critical_stock_alerts,
                pending_purchase_orders_count=len(enhanced_drafts),
                total_waste_saved_estimate=local_drafts.total_waste_saved_estimate
            )
            
        return local_drafts

    except Exception as e:
        logger.error(f"Error calling OpenAI for smart procurement: {e}")
        return local_drafts


def dispatch_po_email(
    tenant_id: int,
    vendor_name: str,
    items: List[dict],
    recipient_email: str
) -> dict:
    """
    Dispatches a purchase order email using Resend SDK if API key is configured,
    or falls back to printing to server logs.
    """
    logger.info(f"Dispatching PO to {vendor_name} for tenant {tenant_id}")
    
    # 1. Format the items list as an HTML table
    table_rows = ""
    for i in items:
        item_name = i.get("item_name", "Unknown Item")
        qty = i.get("recommended_order_qty", 0.0)
        unit = i.get("unit", "units")
        cost = i.get("total_cost", 0.0)
        table_rows += f"<tr><td>{item_name}</td><td>{qty}</td><td>{unit}</td><td>${cost:.2f}</td></tr>"

    html_content = f"""
    <h3>Purchase Order Request — RestoKeeper AI</h3>
    <p>Please deliver the following inventory replenish items to RestoKeeper Bistro:</p>
    <table border="1" cellpadding="6" style="border-collapse: collapse; border-color: #ddd;">
        <thead style="background-color: #f3f4f6;">
            <tr><th>Item Name</th><th>Order Qty</th><th>Unit</th><th>Estimated Cost</th></tr>
        </thead>
        <tbody>
            {table_rows}
        </tbody>
    </table>
    <p>Logged for tenant ID: {tenant_id}. Generated by RestoKeeper Autonomous Reasoner.</p>
    """
    
    import resend
    resend_key = os.getenv("RESEND_API_KEY", "")
    if resend_key and not resend_key.startswith("mock_"):
        try:
            resend.api_key = resend_key
            resend.Emails.send({
                "from": "RestoKeeper AI <orders@restokeeper.com>",
                "to": [recipient_email],
                "subject": f"Purchase Order Request — {vendor_name}",
                "html": html_content
            })
            return {"status": "success", "message": f"PO sent successfully via Resend to {recipient_email}"}
        except Exception as e:
            logger.error(f"Failed to send email via Resend SDK: {e}")
            # Fallback to logs
    
    # Standard development log print fallback
    logger.info(f"=== MOCK PO EMAIL SENT ===")
    logger.info(f"To: {recipient_email}")
    logger.info(f"Vendor: {vendor_name}")
    logger.info(f"Content:\n{html_content}")
    logger.info(f"===========================")
    
    return {"status": "success", "message": f"PO simulated. Email logged to server console for {recipient_email}."}
