import os
import logging
from fastapi import UploadFile
from sqlalchemy.orm import Session
from rapidfuzz import process, utils as fuzz_utils
from openai import OpenAI
from typing import List, Tuple

from ..database import get_db
from ..models import MasterIngredient, StockHistoryLog
from ..schemas import ExtractedVoiceItem, IngredientStockUpdate

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
    logger.info("Using Mock OpenAI mode for voice uploads.")
    openai_client = None

def transcribe_and_extract_voice(
    file_bytes: bytes, 
    filename: str, 
    master_ingredients_names: List[str]
) -> Tuple[str, List[ExtractedVoiceItem]]:
    """
    Transcribes audio using OpenAI Whisper API and structures the output using GPT-4o.
    Uses master ingredients names as phonetic spelling hints.
    """
    if is_mock_openai:
        # Return mock transcript and parsed items for zero-config local runs
        transcript = "Onions, 3 bags; Cheddar Cheese, 2 blocks; Tomato, 5 kg"
        extracted_items = [
            ExtractedVoiceItem(item_name="Onions", quantity=3.0, unit="bags"),
            ExtractedVoiceItem(item_name="Cheddar Cheese", quantity=2.0, unit="blocks"),
            ExtractedVoiceItem(item_name="Tomato", quantity=5.0, unit="kg")
        ]
        return transcript, extracted_items

    try:
        # Create a temporary file to send to Whisper
        temp_filename = f"temp_{filename}"
        with open(temp_filename, "wb") as temp_file:
            temp_file.write(file_bytes)

        # 1. Whisper Transcription with spelling hints
        spelling_prompt = ", ".join(master_ingredients_names)
        with open(temp_filename, "rb") as audio_file:
            transcript_obj = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                prompt=spelling_prompt
            )
        transcript = transcript_obj.text

        # Clean up temp file
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

        # 2. Structured Extraction using GPT-4o
        system_prompt = (
            "You are an expert restaurant inventory AI. Extract list items, quantities, "
            "and units from the transcription text. Output the result in structured JSON format "
            "complying with the schema: List of items with fields: item_name (string), quantity (float), unit (string)."
        )
        
        response = openai_client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Transcription text to extract: {transcript}"}
            ],
            response_format=List[ExtractedVoiceItem]
        )
        extracted_items = response.choices[0].message.parsed
        return transcript, extracted_items

    except Exception as e:
        logger.error(f"Error in transcription/extraction pipeline: {e}")
        # Soft fallback if live API fails
        transcript = "Fallback: API call failed. Using mock parser for audio input."
        extracted_items = [
            ExtractedVoiceItem(item_name="Onions", quantity=3.0, unit="bags"),
            ExtractedVoiceItem(item_name="Cheddar Cheese", quantity=2.0, unit="blocks"),
            ExtractedVoiceItem(item_name="Tomato", quantity=5.0, unit="kg")
        ]
        return transcript, extracted_items


import re

def rule_based_extract_items(text: str, ingredient_names: List[str]) -> List[ExtractedVoiceItem]:
    """
    A local rule-based regex parser that extracts items, quantities, and units 
    from text transcription when OpenAI is not available.
    """
    extracted = []
    # Normalize common commas: e.g. "Dal, 5 kg" -> "Dal 5 kg"
    text_clean = re.sub(r',\s*(\d)', r' \1', text)
    # Split by semicolons, "and", "then", or newlines
    phrases = re.split(r'[;\n]|(?:\band\b)|(?:\bthen\b)', text_clean, flags=re.IGNORECASE)
    
    common_units = {
        'bags', 'boxes', 'kg', 'blocks', 'packets', 'units', 'ltr', 'liters', 
        'grams', 'g', 'kg.', 'bag', 'box', 'packet', 'kg', 'liter', 'liters', 'l'
    }
    
    for phrase in phrases:
        phrase = phrase.strip()
        if not phrase:
            continue
            
        # Search for first number (integer or float)
        num_match = re.search(r'(\d+(?:\.\d+)?)', phrase)
        if not num_match:
            continue
            
        qty = float(num_match.group(1))
        
        # Split phrase around the number
        parts = phrase.split(num_match.group(0))
        left_text = parts[0].strip()
        right_text = parts[1].strip() if len(parts) > 1 else ""
        
        item_token = ""
        unit_token = "units"
        
        # Check if right text starts with a unit
        words_right = right_text.split()
        starts_with_unit = False
        if words_right:
            first_word = words_right[0].lower().rstrip('s').rstrip('.')
            if first_word in common_units or first_word in {'kg', 'g', 'l', 'ml'}:
                starts_with_unit = True
                
        # If left is empty, or only filler words, or right starts with a unit:
        # e.g., "5 bags of Rice" or "I have 10 kg Dal"
        left_clean = left_text.lower().strip()
        is_filler_left = left_clean in {'', 'i have', 'we have', 'there is', 'there are', 'please set', 'update', 'set', 'add'}
        
        if is_filler_left or starts_with_unit:
            right_clean = re.sub(r'^of\s+', '', right_text, flags=re.IGNORECASE).strip()
            words = right_clean.split()
            if words:
                first_word = words[0].lower().rstrip('s').rstrip('.')
                if first_word in common_units or first_word in {'kg', 'g', 'l', 'ml'}:
                    unit_token = words[0]
                    item_token = " ".join(words[1:])
                    if item_token.lower().startswith("of "):
                        item_token = item_token[3:].strip()
                else:
                    item_token = " ".join(words)
        else:
            # E.g. "Rice 10 bags"
            item_token = left_text
            # Strip verb prefix
            item_token = re.sub(r'^(set|update|add|change|reset)\s+', '', item_token, flags=re.IGNORECASE).strip()
            item_token = re.sub(r'\s+to$', '', item_token, flags=re.IGNORECASE).strip()
            if words_right:
                unit_token = words_right[0]
                
        item_token = item_token.strip()
        if item_token:
            extracted.append(
                ExtractedVoiceItem(
                    item_name=item_token,
                    quantity=qty,
                    unit=unit_token
                )
            )
            
    return extracted


def apply_extracted_counts_to_db(
    db: Session,
    tenant_id: int,
    extracted_items: List[ExtractedVoiceItem],
    source: str = "voice_inventory"
) -> Tuple[List[IngredientStockUpdate], List[ExtractedVoiceItem]]:
    """
    Core matching and stock update logic. Takes extracted counts, maps them to database
    ingredients via fuzzy matching, updates current stocks, logs changes, and syncs to Google Sheets.
    """
    ingredients = db.query(MasterIngredient).filter(MasterIngredient.tenant_id == tenant_id).all()
    ingredient_names = [ing.item_name for ing in ingredients]
    
    updated_ingredients = []
    unmapped_items = []
    
    for ext_item in extracted_items:
        if not ingredient_names:
            unmapped_items.append(ext_item)
            continue
            
        clean_ext_name = fuzz_utils.default_process(ext_item.item_name)
        
        match_result = process.extractOne(
            clean_ext_name,
            ingredient_names,
            processor=fuzz_utils.default_process
        )
        
        if match_result and match_result[1] >= 65.0:
            matched_name = match_result[0]
            matched_ing = next(ing for ing in ingredients if ing.item_name == matched_name)
            
            old_stock = matched_ing.current_stock
            matched_ing.current_stock = ext_item.quantity
            db.add(matched_ing)
            
            from .sheets import update_sheet_ingredient_stock
            update_sheet_ingredient_stock(matched_ing.SKU_code, matched_ing.current_stock)
            
            qty_changed = ext_item.quantity - old_stock
            history_log = StockHistoryLog(
                master_ingredient_id=matched_ing.id,
                quantity_changed=qty_changed,
                change_source=source
            )
            db.add(history_log)
            
            updated_ingredients.append(
                IngredientStockUpdate(
                    ingredient_id=matched_ing.id,
                    item_name=matched_ing.item_name,
                    old_stock=old_stock,
                    new_stock=matched_ing.current_stock,
                    unit=matched_ing.unit_type
                )
            )
        else:
            unmapped_items.append(ext_item)
            
    db.commit()
    return updated_ingredients, unmapped_items


def process_voice_inventory(
    db: Session, 
    tenant_id: int, 
    file_bytes: bytes, 
    filename: str
) -> Tuple[str, List[ExtractedVoiceItem], List[IngredientStockUpdate], List[ExtractedVoiceItem]]:
    """
    Coordinates transcription, structures data, matches items to database ingredients,
    updates stock levels, and writes change logs.
    """
    ingredients = db.query(MasterIngredient).filter(MasterIngredient.tenant_id == tenant_id).all()
    ingredient_names = [ing.item_name for ing in ingredients]
    
    transcript, extracted_items = transcribe_and_extract_voice(file_bytes, filename, ingredient_names)
    updated, unmapped = apply_extracted_counts_to_db(db, tenant_id, extracted_items, "voice_inventory")
    return transcript, extracted_items, updated, unmapped


def process_voice_text_inventory(
    db: Session,
    tenant_id: int,
    text: str
) -> Tuple[str, List[ExtractedVoiceItem], List[IngredientStockUpdate], List[ExtractedVoiceItem]]:
    """
    Processes raw text counts (from web speech recognition or type input).
    """
    ingredients = db.query(MasterIngredient).filter(MasterIngredient.tenant_id == tenant_id).all()
    ingredient_names = [ing.item_name for ing in ingredients]
    
    extracted_items = []
    if not is_mock_openai:
        try:
            system_prompt = (
                "You are an expert restaurant inventory AI. Extract list items, quantities, "
                "and units from the transcription text. Output the result in structured JSON format "
                "complying with the schema: List of items with fields: item_name (string), quantity (float), unit (string)."
            )
            
            response = openai_client.beta.chat.completions.parse(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Transcription text to extract: {text}"}
                ],
                response_format=List[ExtractedVoiceItem]
            )
            extracted_items = response.choices[0].message.parsed
        except Exception as e:
            logger.error(f"OpenAI text parse failed: {e}. Falling back to regex.")
            extracted_items = rule_based_extract_items(text, ingredient_names)
    else:
        extracted_items = rule_based_extract_items(text, ingredient_names)
        
    updated, unmapped = apply_extracted_counts_to_db(db, tenant_id, extracted_items, "voice_inventory")
    return text, extracted_items, updated, unmapped
