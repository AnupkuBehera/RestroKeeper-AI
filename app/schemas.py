from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

# Tenant and User basic schemas
class TenantBase(BaseModel):
    restaurant_name: str

class TenantCreate(TenantBase):
    pass

class TenantResponse(TenantBase):
    id: int
    created_at: datetime
    class Config:
        from_attributes = True

class UserBase(BaseModel):
    name: str
    email: str
    role: str

class UserResponse(UserBase):
    id: int
    tenant_id: int
    class Config:
        from_attributes = True


# Ingredient schemas
class IngredientBase(BaseModel):
    SKU_code: str
    item_name: str
    current_stock: float
    safety_par_level: float
    unit_type: str
    cost_per_unit: float
    vendor_name: str

class IngredientUpdatePayload(BaseModel):
    current_stock: Optional[float] = None
    item_name: Optional[str] = None
    safety_par_level: Optional[float] = None
    unit_type: Optional[str] = None
    cost_per_unit: Optional[float] = None
    vendor_name: Optional[str] = None

class IngredientResponse(IngredientBase):
    id: int
    tenant_id: int
    class Config:
        from_attributes = True

class IngredientStockUpdate(BaseModel):
    ingredient_id: int
    item_name: str
    old_stock: float
    new_stock: float
    unit: str


# Voice Inventory schemas
class ExtractedVoiceItem(BaseModel):
    item_name: str
    quantity: float
    unit: str

class VoiceUploadResponse(BaseModel):
    transcript: str
    extracted_items: List[ExtractedVoiceItem]
    updated_ingredients: List[IngredientStockUpdate]
    unmapped_items: List[ExtractedVoiceItem]


# Invoice OCR schemas
class ExtractedInvoiceItem(BaseModel):
    item_name: str
    quantity: float
    unit_price: float
    price_anomaly: bool = False
    previous_price: Optional[float] = None

class InvoiceUploadResponse(BaseModel):
    vendor_name: str
    invoice_number: str
    total_amount: float
    items: List[ExtractedInvoiceItem]


# Smart Procurement schemas
class DraftOrderItem(BaseModel):
    item_name: str
    current_stock: float
    safety_par_level: float
    forecasted_demand: float
    recommended_order_qty: float
    unit: str
    cost_per_unit: float
    total_cost: float

class VendorDraftOrder(BaseModel):
    vendor_name: str
    items: List[DraftOrderItem]
    total_order_cost: float

class DraftOrdersResponse(BaseModel):
    draft_orders: List[VendorDraftOrder]
    total_procurement_cost: float
    critical_stock_alerts: int
    pending_purchase_orders_count: int
    total_waste_saved_estimate: float

class VoiceTextPayload(BaseModel):
    text: str


# Authentication Schemas
class UserSignup(BaseModel):
    name: str
    email: str
    password: str
    restaurant_name: str


class UserLogin(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    email: Optional[str] = None
    user_id: Optional[int] = None
    tenant_id: Optional[int] = None


# Recipe & Menu Schemas
class RecipeRequirementBase(BaseModel):
    ingredient_id: int
    quantity_required: float


class RecipeRequirementResponse(RecipeRequirementBase):
    id: int
    menu_item_id: int
    class Config:
        from_attributes = True


class MenuItemBase(BaseModel):
    name: str
    price: float


class MenuItemCreate(MenuItemBase):
    recipes: List[RecipeRequirementBase]


class MenuItemResponse(MenuItemBase):
    id: int
    tenant_id: int
    recipes: List[RecipeRequirementResponse] = []
    class Config:
        from_attributes = True


# Sales Schemas
class SaleCreate(BaseModel):
    menu_item_id: int
    quantity_sold: int


class SaleResponse(BaseModel):
    id: int
    tenant_id: int
    menu_item_id: int
    quantity_sold: int
    sold_at: datetime
    class Config:
        from_attributes = True


# Stock Batch & Expiry Schemas
class StockBatchBase(BaseModel):
    master_ingredient_id: int
    quantity: float
    expiry_date: datetime


class StockBatchResponse(StockBatchBase):
    id: int
    received_date: datetime
    class Config:
        from_attributes = True


# Wastage Schemas
class WastageCreate(BaseModel):
    master_ingredient_id: int
    quantity_wasted: float
    reason: Optional[str] = None


class WastageResponse(WastageCreate):
    id: int
    logged_at: datetime
    class Config:
        from_attributes = True
