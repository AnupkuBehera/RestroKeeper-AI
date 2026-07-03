import os
import logging
import base64
import hmac
import hashlib
import json
import time
from datetime import datetime, timedelta
from fastapi import FastAPI, Depends, UploadFile, File, Form, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import List

from .database import engine, Base, get_db
from .models import Tenant, User, MasterIngredient, StockHistoryLog, MenuItem, RecipeRequirement, StockBatch, SaleLog, WastageLog
from .schemas import (
    VoiceUploadResponse, InvoiceUploadResponse, DraftOrdersResponse, 
    IngredientResponse, IngredientBase, IngredientUpdatePayload, VoiceTextPayload,
    UserSignup, UserLogin, Token, TokenData, MenuItemCreate, MenuItemResponse,
    SaleCreate, SaleResponse, StockBatchBase, StockBatchResponse, WastageCreate, WastageResponse
)
from .services.audio import process_voice_inventory
from .services.vision import process_invoice_ocr
from .services.procurement import generate_smart_procurement_drafts
from .services.sheets import (
    sync_ingredients_from_sheet,
    update_sheet_ingredient_stock,
    add_sheet_ingredient,
    log_po_to_sheet,
    log_audit_to_sheet
)

# JWT Auth configurations
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "super-secret-restokeeper-key-9812")
security = HTTPBearer(auto_error=False)

def hash_password(password: str) -> str:
    """Hash password using PBKDF2 with SHA256."""
    salt = "rk_salt_"
    hashed = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    )
    return hashed.hex()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password by comparing hashes."""
    if not hashed_password:
        return False
    return hash_password(plain_password) == hashed_password

def create_access_token(data: dict, expires_in: int = 86400) -> str:
    """Create a signed JWT token."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = data.copy()
    payload["exp"] = int(time.time()) + expires_in
    
    # Base64url encode header and payload
    header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    
    # Sign
    signature_base = f"{header_b64}.{payload_b64}".encode()
    signature = hmac.new(SECRET_KEY.encode(), signature_base, hashlib.sha256).digest()
    signature_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    
    return f"{header_b64}.{payload_b64}.{signature_b64}"

def decode_access_token(token: str) -> dict:
    """Decode and verify a JWT token."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, signature_b64 = parts
        
        # Verify signature
        signature_base = f"{header_b64}.{payload_b64}".encode()
        expected_sig = hmac.new(SECRET_KEY.encode(), signature_base, hashlib.sha256).digest()
        sig_padding = len(signature_b64) % 4
        sig_b64_padded = signature_b64 + ("=" * (4 - sig_padding) if sig_padding else "")
        sig_bytes = base64.urlsafe_b64decode(sig_b64_padded)
        
        if not hmac.compare_digest(sig_bytes, expected_sig):
            return None
            
        # Decode payload
        payload_padding = len(payload_b64) % 4
        payload_b64_padded = payload_b64 + ("=" * (4 - payload_padding) if payload_padding else "")
        payload = json.loads(base64.urlsafe_b64decode(payload_b64_padded).decode())
        
        # Verify expiration
        if payload.get("exp", 0) < time.time():
            return None
            
        return payload
    except Exception:
        return None

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> TokenData:
    """
    FastAPI dependency to extract JWT and return user and tenant context.
    Falls back to a default sandbox tenant context if no credentials are provided,
    ensuring local backward compatibility.
    """
    default_context = TokenData(email="manager@restokeeper.com", user_id=1, tenant_id=1)
    if not credentials:
        return default_context
        
    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired access token")
        
    return TokenData(
        email=payload.get("sub"),
        user_id=payload.get("user_id"),
        tenant_id=payload.get("tenant_id")
    )

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create database tables
Base.metadata.create_all(bind=engine)

def get_current_tenant_id(
    x_tenant_id: str = Header(default="1"),
    current_user: TokenData = Depends(get_current_user)
) -> int:
    """
    Resolves tenant_id from JWT if present, falling back to X-Tenant-ID header.
    """
    # If the user authenticated via JWT and it's not the default fallback tenant, use that
    if current_user.email != "manager@restokeeper.com" or x_tenant_id == "1":
        return current_user.tenant_id
        
    try:
        return int(x_tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Tenant-ID header must be an integer")

app = FastAPI(
    title="RestoKeeper AI Backend",
    description="FastAPI + Supabase backend for AI voice inventory and smart procurement",
    version="1.0.0"
)

# Enable CORS for frontend communications
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify frontend URL (e.g. http://localhost:4321)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Dependency to seed default data if empty
def seed_database_if_empty(db: Session):
    tenant_count = db.query(Tenant).count()
    if tenant_count == 0:
        logger.info("Database is empty. Seeding default tenant, users, and ingredients...")
        
        # 1. Create Default Tenant
        default_tenant = Tenant(restaurant_name="RestoKeeper Bistro")
        db.add(default_tenant)
        db.commit()
        db.refresh(default_tenant)

        # 2. Create Default Users
        manager_user = User(
            tenant_id=default_tenant.id,
            name="John Doe",
            email="manager@restokeeper.com",
            role="manager"
        )
        staff_user = User(
            tenant_id=default_tenant.id,
            name="Sarah Smith",
            email="kitchen@restokeeper.com",
            role="kitchen_staff"
        )
        db.add(manager_user)
        db.add(staff_user)
        db.commit()
        tenant_id = default_tenant.id
    else:
        first_tenant = db.query(Tenant).first()
        tenant_id = first_tenant.id if first_tenant else 1

    # Check if standard ingredient "Atta" exists, if not, delete old ingredients and re-seed
    atta_exists = db.query(MasterIngredient).filter(
        MasterIngredient.tenant_id == tenant_id,
        MasterIngredient.item_name == "Atta"
    ).first()
    
    if not atta_exists:
        logger.info("Standard ingredients missing. Seeding Atta, Dal, Rice, etc...")
        # Clear existing logs and ingredients for a clean reset
        db.query(StockHistoryLog).delete()
        db.query(MasterIngredient).filter(MasterIngredient.tenant_id == tenant_id).delete()
        db.commit()
        
        default_ingredients = [
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-ATT-01",
                item_name="Atta",
                current_stock=2.0,
                safety_par_level=5.0,
                unit_type="bags",
                cost_per_unit=12.00,
                vendor_name="Desi Grains Co."
            ),
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-DAL-02",
                item_name="Dal",
                current_stock=8.0,
                safety_par_level=10.0,
                unit_type="kg",
                cost_per_unit=4.50,
                vendor_name="Desi Grains Co."
            ),
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-RIC-03",
                item_name="Rice",
                current_stock=3.0,
                safety_par_level=8.0,
                unit_type="bags",
                cost_per_unit=18.00,
                vendor_name="Desi Grains Co."
            ),
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-ONN-04",
                item_name="Onions",
                current_stock=18.0,
                safety_par_level=15.0,
                unit_type="kg",
                cost_per_unit=2.50,
                vendor_name="Fresh Produce Co."
            ),
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-POT-05",
                item_name="Potato",
                current_stock=12.0,
                safety_par_level=20.0,
                unit_type="kg",
                cost_per_unit=1.80,
                vendor_name="Fresh Produce Co."
            ),
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-SUG-06",
                item_name="Sugar",
                current_stock=4.0,
                safety_par_level=10.0,
                unit_type="kg",
                cost_per_unit=3.00,
                vendor_name="Sysco"
            ),
        ]
        
        for ing in default_ingredients:
            db.add(ing)
        db.commit()
        logger.info("Database seeding completed.")


# Seed database at server start
db = next(get_db())
try:
    seed_database_if_empty(db)
finally:
    db.close()


@app.get("/")
@app.get("/api/health")
def read_root():
    return {
        "status": "online",
        "service": "RestoKeeper AI Backend",
        "database": str(engine.url).split("@")[-1]  # Hide credentials, show DB host
    }


@app.post("/api/auth/signup", response_model=Token)
def signup(payload: UserSignup, db: Session = Depends(get_db)):
    # Check if email is already taken
    existing_user = db.query(User).filter(User.email == payload.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email is already registered")
        
    # 1. Create a new Tenant
    new_tenant = Tenant(restaurant_name=payload.restaurant_name)
    db.add(new_tenant)
    db.commit()
    db.refresh(new_tenant)
    
    # 2. Create the User
    hashed_pwd = hash_password(payload.password)
    new_user = User(
        tenant_id=new_tenant.id,
        name=payload.name,
        email=payload.email,
        role="manager",
        password_hash=hashed_pwd
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    # 3. Seed new ingredients for this new tenant
    default_ingredients = [
        MasterIngredient(
            tenant_id=new_tenant.id,
            SKU_code="SKU-ATT-01",
            item_name="Atta",
            current_stock=2.0,
            safety_par_level=5.0,
            unit_type="bags",
            cost_per_unit=12.00,
            vendor_name="Desi Grains Co."
        ),
        MasterIngredient(
            tenant_id=new_tenant.id,
            SKU_code="SKU-DAL-02",
            item_name="Dal",
            current_stock=8.0,
            safety_par_level=10.0,
            unit_type="kg",
            cost_per_unit=4.50,
            vendor_name="Desi Grains Co."
        ),
        MasterIngredient(
            tenant_id=new_tenant.id,
            SKU_code="SKU-RIC-03",
            item_name="Rice",
            current_stock=3.0,
            safety_par_level=8.0,
            unit_type="bags",
            cost_per_unit=18.00,
            vendor_name="Desi Grains Co."
        ),
        MasterIngredient(
            tenant_id=new_tenant.id,
            SKU_code="SKU-ONN-04",
            item_name="Onions",
            current_stock=18.0,
            safety_par_level=15.0,
            unit_type="kg",
            cost_per_unit=2.50,
            vendor_name="Fresh Produce Co."
        ),
        MasterIngredient(
            tenant_id=new_tenant.id,
            SKU_code="SKU-POT-05",
            item_name="Potato",
            current_stock=12.0,
            safety_par_level=20.0,
            unit_type="kg",
            cost_per_unit=1.80,
            vendor_name="Fresh Produce Co."
        ),
        MasterIngredient(
            tenant_id=new_tenant.id,
            SKU_code="SKU-SUG-06",
            item_name="Sugar",
            current_stock=4.0,
            safety_par_level=10.0,
            unit_type="kg",
            cost_per_unit=3.00,
            vendor_name="Sysco"
        ),
    ]
    for ing in default_ingredients:
        db.add(ing)
    db.commit()
    
    # 4. Generate JWT
    token_data = {
        "sub": new_user.email,
        "user_id": new_user.id,
        "tenant_id": new_tenant.id
    }
    token = create_access_token(token_data)
    return {"access_token": token, "token_type": "bearer"}


@app.post("/api/auth/login", response_model=Token)
def login(payload: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
        
    token_data = {
        "sub": user.email,
        "user_id": user.id,
        "tenant_id": user.tenant_id
    }
    token = create_access_token(token_data)
    return {"access_token": token, "token_type": "bearer"}


@app.get("/api/menu", response_model=List[MenuItemResponse])
def get_menu(
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """Fetch all menu items along with their recipe requirements."""
    items = db.query(MenuItem).filter(MenuItem.tenant_id == tenant_id).all()
    return items


@app.post("/api/menu", response_model=MenuItemResponse)
def create_menu_item(
    payload: MenuItemCreate,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """Create a menu item and define its recipe requirements."""
    # Check if menu item already exists
    existing = db.query(MenuItem).filter(
        MenuItem.tenant_id == tenant_id,
        MenuItem.name == payload.name
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Menu item already exists")
        
    new_item = MenuItem(
        tenant_id=tenant_id,
        name=payload.name,
        price=payload.price
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    
    # Add recipe requirements
    for recipe_req in payload.recipes:
        ing = db.query(MasterIngredient).filter(
            MasterIngredient.id == recipe_req.ingredient_id,
            MasterIngredient.tenant_id == tenant_id
        ).first()
        if not ing:
            raise HTTPException(
                status_code=400, 
                detail=f"Ingredient ID {recipe_req.ingredient_id} not found"
            )
            
        req = RecipeRequirement(
            menu_item_id=new_item.id,
            ingredient_id=recipe_req.ingredient_id,
            quantity_required=recipe_req.quantity_required
        )
        db.add(req)
    db.commit()
    db.refresh(new_item)
    return new_item


@app.post("/api/sales", response_model=SaleResponse)
def log_sale(
    payload: SaleCreate,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """
    Logs menu item sales, deducts raw ingredients from batches using FIFO, 
    updates master stock level, and syncs to Google Sheets.
    """
    menu_item = db.query(MenuItem).filter(
        MenuItem.id == payload.menu_item_id,
        MenuItem.tenant_id == tenant_id
    ).first()
    if not menu_item:
        raise HTTPException(status_code=404, detail="Menu item not found")
        
    for recipe_req in menu_item.recipes:
        total_needed = recipe_req.quantity_required * payload.quantity_sold
        ing = recipe_req.ingredient
        
        remaining = total_needed
        batches = db.query(StockBatch).filter(
            StockBatch.master_ingredient_id == ing.id
        ).order_by(StockBatch.expiry_date.asc()).all()
        
        for batch in batches:
            if remaining <= 0:
                break
            if batch.quantity >= remaining:
                batch.quantity -= remaining
                remaining = 0.0
                db.add(batch)
            else:
                remaining -= batch.quantity
                db.delete(batch)
                
        ing.current_stock = max(0.0, ing.current_stock - total_needed)
        db.add(ing)
        
        history_log = StockHistoryLog(
            master_ingredient_id=ing.id,
            quantity_changed=-total_needed,
            change_source="sales_deduction"
        )
        db.add(history_log)
        
        update_sheet_ingredient_stock(ing.SKU_code, ing.current_stock)
        
    new_sale = SaleLog(
        tenant_id=tenant_id,
        menu_item_id=payload.menu_item_id,
        quantity_sold=payload.quantity_sold
    )
    db.add(new_sale)
    db.commit()
    db.refresh(new_sale)
    
    return new_sale


@app.post("/api/inventory/batches", response_model=StockBatchResponse)
def create_stock_batch(
    payload: StockBatchBase,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """Register a new stock batch for an ingredient, updating its master stock level."""
    ing = db.query(MasterIngredient).filter(
        MasterIngredient.id == payload.master_ingredient_id,
        MasterIngredient.tenant_id == tenant_id
    ).first()
    if not ing:
        raise HTTPException(status_code=404, detail="Ingredient not found")
        
    batch = StockBatch(
        master_ingredient_id=payload.master_ingredient_id,
        quantity=payload.quantity,
        expiry_date=payload.expiry_date
    )
    db.add(batch)
    
    # Update master stock
    ing.current_stock += payload.quantity
    db.add(ing)
    
    # Log stock history
    history_log = StockHistoryLog(
        master_ingredient_id=ing.id,
        quantity_changed=payload.quantity,
        change_source="batch_received"
    )
    db.add(history_log)
    db.commit()
    db.refresh(batch)
    
    # Sync to Sheets
    update_sheet_ingredient_stock(ing.SKU_code, ing.current_stock)
    
    return batch


@app.get("/api/inventory/batches", response_model=List[StockBatchResponse])
def get_stock_batches(
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """List all stock batches for the current tenant's ingredients."""
    batches = db.query(StockBatch).join(MasterIngredient).filter(
        MasterIngredient.tenant_id == tenant_id
    ).all()
    return batches


@app.get("/api/inventory/expiry-alerts", response_model=List[StockBatchResponse])
def get_expiry_alerts(
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """Fetch ingredient batches expiring within the next 7 days."""
    threshold = datetime.now() + timedelta(days=7)
    batches = db.query(StockBatch).join(MasterIngredient).filter(
        MasterIngredient.tenant_id == tenant_id,
        StockBatch.expiry_date <= threshold,
        StockBatch.quantity > 0.0
    ).order_by(StockBatch.expiry_date.asc()).all()
    return batches


@app.post("/api/inventory/wastage", response_model=WastageResponse)
def log_wastage(
    payload: WastageCreate,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """Logs spoiled or wasted stock, deducting it from batches via FIFO and syncing to Sheets."""
    ing = db.query(MasterIngredient).filter(
        MasterIngredient.id == payload.master_ingredient_id,
        MasterIngredient.tenant_id == tenant_id
    ).first()
    if not ing:
        raise HTTPException(status_code=404, detail="Ingredient not found")
        
    qty = payload.quantity_wasted
    
    # Deduct from batches using FIFO
    remaining = qty
    batches = db.query(StockBatch).filter(
        StockBatch.master_ingredient_id == ing.id
    ).order_by(StockBatch.expiry_date.asc()).all()
    
    for batch in batches:
        if remaining <= 0:
            break
        if batch.quantity >= remaining:
            batch.quantity -= remaining
            remaining = 0.0
            db.add(batch)
        else:
            remaining -= batch.quantity
            db.delete(batch)
            
    # Deduct from master stock
    ing.current_stock = max(0.0, ing.current_stock - qty)
    db.add(ing)
    
    # Log stock history
    history_log = StockHistoryLog(
        master_ingredient_id=ing.id,
        quantity_changed=-qty,
        change_source="wastage"
    )
    db.add(history_log)
    
    # Create wastage log entry
    wastage = WastageLog(
        master_ingredient_id=payload.master_ingredient_id,
        quantity_wasted=qty,
        reason=payload.reason
    )
    db.add(wastage)
    db.commit()
    db.refresh(wastage)
    
    # Sync to Sheets
    update_sheet_ingredient_stock(ing.SKU_code, ing.current_stock)
    
    return wastage


@app.get("/api/ingredients", response_model=List[IngredientResponse])
def get_ingredients(tenant_id: int = Depends(get_current_tenant_id), db: Session = Depends(get_db)):
    """Fetch current master ingredients for a tenant, syncing from Google Sheets first."""
    sync_ingredients_from_sheet(db=db, tenant_id=tenant_id)
    ingredients = db.query(MasterIngredient).filter(MasterIngredient.tenant_id == tenant_id).all()
    return ingredients


@app.get("/api/sheets/config")
def get_sheets_config():
    """Returns Google Sheets setup configuration details to the frontend status badge."""
    from .services.sheets import is_sheets_mock, SHEET_ID, CREDENTIALS_JSON
    
    # Extract service account email if credentials JSON is set
    share_email = "not-configured"
    if CREDENTIALS_JSON:
        try:
            if CREDENTIALS_JSON.strip().startswith("{"):
                creds_info = json.loads(CREDENTIALS_JSON)
                share_email = creds_info.get("client_email", "invalid-client-email")
            else:
                # Try to load file
                with open(CREDENTIALS_JSON, "r") as f:
                    creds_info = json.load(f)
                    share_email = creds_info.get("client_email", "invalid-client-email")
        except Exception:
            share_email = "invalid-credentials-format"
            
    return {
        "is_mock": is_sheets_mock,
        "sheet_id": SHEET_ID or "not-configured",
        "share_email": share_email
    }


@app.post("/api/ingredients", response_model=IngredientResponse)
def create_ingredient(
    payload: IngredientBase,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """Create a new master ingredient and sync to Google Sheets."""
    # Check if SKU already exists for the tenant
    existing = db.query(MasterIngredient).filter(
        MasterIngredient.tenant_id == tenant_id,
        MasterIngredient.SKU_code == payload.SKU_code
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ingredient SKU already exists")

    new_ing = MasterIngredient(
        tenant_id=tenant_id,
        SKU_code=payload.SKU_code,
        item_name=payload.item_name,
        current_stock=payload.current_stock,
        safety_par_level=payload.safety_par_level,
        unit_type=payload.unit_type,
        cost_per_unit=payload.cost_per_unit,
        vendor_name=payload.vendor_name
    )
    db.add(new_ing)
    db.commit()
    db.refresh(new_ing)

    # Sync row creation to Google Sheets
    add_sheet_ingredient(
        sku=payload.SKU_code,
        name=payload.item_name,
        stock=payload.current_stock,
        par=payload.safety_par_level,
        unit=payload.unit_type,
        cost=payload.cost_per_unit,
        vendor=payload.vendor_name
    )
    
    return new_ing


@app.post("/api/ingredients/clear-stocks")
def clear_all_ingredient_stocks(
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """Sets current stock level of all ingredients to 0.0 to enable fresh count logging."""
    try:
        # Clear history logs
        db.query(StockHistoryLog).delete()
        
        # Set all stocks to 0.0
        ingredients = db.query(MasterIngredient).filter(MasterIngredient.tenant_id == tenant_id).all()
        for ing in ingredients:
            ing.current_stock = 0.0
            db.add(ing)
            # Sync stock cell to Google Sheet
            update_sheet_ingredient_stock(ing.SKU_code, 0.0)
        db.commit()
        return {"status": "success", "message": "All stock levels cleared to 0.0 for a fresh count."}
    except Exception as e:
        logger.error(f"Failed to clear stocks: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/inventory/voice-upload", response_model=VoiceUploadResponse)
async def upload_voice_inventory(
    file: UploadFile = File(...),
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """
    Accepts voice count recordings (.mp3/.m4a/.wav), transcribes speech with Whisper, 
    extracts structured counts using GPT-4o, and syncs stock counts.
    """
    try:
        file_bytes = await file.read()
        transcript, extracted_items, updated_ingredients, unmapped_items = process_voice_inventory(
            db=db,
            tenant_id=tenant_id,
            file_bytes=file_bytes,
            filename=file.filename
        )
        return VoiceUploadResponse(
            transcript=transcript,
            extracted_items=extracted_items,
            updated_ingredients=updated_ingredients,
            unmapped_items=unmapped_items
        )
    except Exception as e:
        logger.error(f"Voice upload execution error: {e}")
        raise HTTPException(status_code=500, detail=f"Voice process failed: {str(e)}")


@app.post("/api/procurement/upload-invoice", response_model=InvoiceUploadResponse)
async def upload_supplier_invoice(
    file: UploadFile = File(...),
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """
    Accepts image files of supplier invoices, runs GPT-4o Vision OCR, and performs price audits.
    """
    try:
        file_bytes = await file.read()
        result = process_invoice_ocr(
            db=db,
            tenant_id=tenant_id,
            file_bytes=file_bytes,
            filename=file.filename,
            content_type=file.content_type
        )
        # Log parsed price audit entries to Google Sheets
        log_audit_to_sheet(
            invoice_number=result.invoice_number,
            vendor_name=result.vendor_name,
            items=[
                {
                    "item_name": item.item_name,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "price_anomaly": item.price_anomaly
                }
                for item in result.items
            ]
        )
        return result
    except Exception as e:
        logger.error(f"Invoice OCR execution error: {e}")
        raise HTTPException(status_code=500, detail=f"Invoice OCR process failed: {str(e)}")


@app.get("/api/procurement/draft-orders", response_model=DraftOrdersResponse)
def get_draft_purchase_orders(tenant_id: int = Depends(get_current_tenant_id), db: Session = Depends(get_db)):
    """
    Aggregates current stock level deficits against forecasted consumptions and 
    generates draft supplier purchase orders grouped by vendor using GPT-4o reasoning.
    """
    try:
        result = generate_smart_procurement_drafts(db=db, tenant_id=tenant_id)
        return result
    except Exception as e:
        logger.error(f"Procurement draft generation error: {e}")
        raise HTTPException(status_code=500, detail=f"Procurement draft failed: {str(e)}")


from pydantic import BaseModel

class POSendRequest(BaseModel):
    vendor_name: str
    items: List[dict]
    recipient_email: str

@app.post("/api/procurement/send-po")
def send_purchase_order(
    payload: POSendRequest,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """
    Approves the purchase order draft and dispatches the email.
    """
    try:
        from .services.procurement import dispatch_po_email
        result = dispatch_po_email(
            tenant_id=tenant_id,
            vendor_name=payload.vendor_name,
            items=payload.items,
            recipient_email=payload.recipient_email
        )
        # Log approved purchase order to Google Sheets
        log_po_to_sheet(
            vendor_name=payload.vendor_name,
            items=payload.items
        )
        return result
    except Exception as e:
        logger.error(f"Failed to dispatch PO: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ingredients/reset")
def reset_ingredient_stocks(tenant_id: int = Depends(get_current_tenant_id), db: Session = Depends(get_db)):
    """
    Utility endpoint to reset ingredients back to default deficit levels.
    """
    try:
        # Delete logs
        db.query(StockHistoryLog).delete()
        
        # Reset master stocks to default values
        ingredients = db.query(MasterIngredient).filter(MasterIngredient.tenant_id == tenant_id).all()
        for ing in ingredients:
            db.delete(ing)
        db.commit()
        
        default_ingredients = [
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-ATT-01",
                item_name="Atta",
                current_stock=2.0,
                safety_par_level=5.0,
                unit_type="bags",
                cost_per_unit=12.00,
                vendor_name="Desi Grains Co."
            ),
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-DAL-02",
                item_name="Dal",
                current_stock=8.0,
                safety_par_level=10.0,
                unit_type="kg",
                cost_per_unit=4.50,
                vendor_name="Desi Grains Co."
            ),
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-RIC-03",
                item_name="Rice",
                current_stock=3.0,
                safety_par_level=8.0,
                unit_type="bags",
                cost_per_unit=18.00,
                vendor_name="Desi Grains Co."
            ),
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-ONN-04",
                item_name="Onions",
                current_stock=18.0,
                safety_par_level=15.0,
                unit_type="kg",
                cost_per_unit=2.50,
                vendor_name="Fresh Produce Co."
            ),
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-POT-05",
                item_name="Potato",
                current_stock=12.0,
                safety_par_level=20.0,
                unit_type="kg",
                cost_per_unit=1.80,
                vendor_name="Fresh Produce Co."
            ),
            MasterIngredient(
                tenant_id=tenant_id,
                SKU_code="SKU-SUG-06",
                item_name="Sugar",
                current_stock=4.0,
                safety_par_level=10.0,
                unit_type="kg",
                cost_per_unit=3.00,
                vendor_name="Sysco"
            ),
        ]
        
        for ing in default_ingredients:
            db.add(ing)
            # Sync to sheet
            update_sheet_ingredient_stock(ing.SKU_code, ing.current_stock)
            
        db.commit()
        return {"status": "success", "message": "Stocks reset to initial default values."}
    except Exception as e:
        logger.error(f"Reset stock levels failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/ingredients/{ingredient_id}", response_model=IngredientResponse)
def update_ingredient(
    ingredient_id: int,
    payload: IngredientUpdatePayload,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """Update an ingredient's properties and sync to Google Sheets."""
    ing = db.query(MasterIngredient).filter(
        MasterIngredient.id == ingredient_id,
        MasterIngredient.tenant_id == tenant_id
    ).first()
    if not ing:
        raise HTTPException(status_code=404, detail="Ingredient not found")
        
    old_stock = ing.current_stock
    
    if payload.current_stock is not None:
        ing.current_stock = payload.current_stock
    if payload.item_name is not None:
        ing.item_name = payload.item_name
    if payload.safety_par_level is not None:
        ing.safety_par_level = payload.safety_par_level
    if payload.unit_type is not None:
        ing.unit_type = payload.unit_type
    if payload.cost_per_unit is not None:
        ing.cost_per_unit = payload.cost_per_unit
    if payload.vendor_name is not None:
        ing.vendor_name = payload.vendor_name
        
    db.commit()
    db.refresh(ing)
    
    # Log stock history if stock changed
    if payload.current_stock is not None and payload.current_stock != old_stock:
        qty_changed = payload.current_stock - old_stock
        history_log = StockHistoryLog(
            master_ingredient_id=ing.id,
            quantity_changed=qty_changed,
            change_source="manual_input"
        )
        db.add(history_log)
        db.commit()
        
        # Sync stock update to Google Sheet
        update_sheet_ingredient_stock(ing.SKU_code, ing.current_stock)
        
    return ing


@app.post("/api/inventory/voice-text-upload", response_model=VoiceUploadResponse)
def process_voice_text_endpoint(
    payload: VoiceTextPayload,
    tenant_id: int = Depends(get_current_tenant_id),
    db: Session = Depends(get_db)
):
    """
    Accepts text transcription, parses counts (regex fallback or OpenAI), 
    updates SQL DB and Google Sheets.
    """
    try:
        from .services.audio import process_voice_text_inventory
        transcript, extracted_items, updated_ingredients, unmapped_items = process_voice_text_inventory(
            db=db,
            tenant_id=tenant_id,
            text=payload.text
        )
        return VoiceUploadResponse(
            transcript=transcript,
            extracted_items=extracted_items,
            updated_ingredients=updated_ingredients,
            unmapped_items=unmapped_items
        )
    except Exception as e:
        logger.error(f"Voice text process error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Duplicate /api routes to support Vercel serverless prefix stripping
for route in list(app.router.routes):
    if hasattr(route, "path") and route.path.startswith("/api/"):
        new_path = route.path[4:]  # Strips '/api', resulting in '/...'
        if not any(r.path == new_path for r in app.router.routes):
            app.router.add_api_route(
                new_path,
                route.endpoint,
                methods=route.methods,
                response_model=getattr(route, "response_model", None),
                dependencies=getattr(route, "dependencies", []),
                summary=getattr(route, "summary", None),
                description=getattr(route, "description", None),
                response_description=getattr(route, "response_description", "Successful Response"),
                tags=getattr(route, "tags", None),
                deprecated=getattr(route, "deprecated", False),
            )

