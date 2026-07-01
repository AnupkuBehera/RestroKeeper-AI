from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base

class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    restaurant_name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    users = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    ingredients = relationship("MasterIngredient", back_populates="tenant", cascade="all, delete-orphan")
    invoices = relationship("SupplierInvoice", back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    role = Column(String, nullable=False)  # manager, kitchen_staff

    tenant = relationship("Tenant", back_populates="users")


class MasterIngredient(Base):
    __tablename__ = "master_ingredients"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    SKU_code = Column(String, nullable=False)
    item_name = Column(String, nullable=False)
    current_stock = Column(Float, default=0.0)
    safety_par_level = Column(Float, default=0.0)
    unit_type = Column(String, nullable=False)  # bags, boxes, kg, blocks, etc.
    cost_per_unit = Column(Float, default=0.0)
    vendor_name = Column(String, nullable=False, default="Generic Supplier")  # Crucial for PO grouping

    tenant = relationship("Tenant", back_populates="ingredients")
    history_logs = relationship("StockHistoryLog", back_populates="ingredient", cascade="all, delete-orphan")


class StockHistoryLog(Base):
    __tablename__ = "stock_history_logs"

    id = Column(Integer, primary_key=True, index=True)
    master_ingredient_id = Column(Integer, ForeignKey("master_ingredients.id", ondelete="CASCADE"), nullable=False)
    quantity_changed = Column(Float, nullable=False)
    change_source = Column(String, nullable=False)  # "voice_inventory", "invoice_ocr"
    logged_at = Column(DateTime(timezone=True), server_default=func.now())

    ingredient = relationship("MasterIngredient", back_populates="history_logs")


class SupplierInvoice(Base):
    __tablename__ = "supplier_invoices"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    vendor_name = Column(String, nullable=False)
    invoice_number = Column(String, nullable=False)
    total_amount = Column(Float, nullable=False)
    issued_date = Column(DateTime(timezone=True), server_default=func.now())

    tenant = relationship("Tenant", back_populates="invoices")
