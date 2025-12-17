# tools/rag_food_tool.py

import json
import os
from typing import Any, Dict, List

import pandas as pd
from dotenv import load_dotenv
from langchain_community.vectorstores import FAISS
from langchain_mistralai import MistralAIEmbeddings
from langchain_core.documents import Document

load_dotenv()

EXCEL_PATH = os.getenv("FOOD_CF_EXCEL_PATH", "sustainable_life.xlsx")
EXCEL_SHEET = "SEL CF for users"
ITEM_COL = "Food commodity ITEM"
CF_COL = "Carbon Footprint kg CO2eq/kg or l of food ITEM"
EMBEDDING_MODEL_NAME = os.getenv("MISTRAL_EMBEDDING_MODEL", "mistral-embed")
SIMILARITY_THRESHOLD = 0.60
STORE_DIR = "food_embeddings_store"

_vectorstore = None
_df = None


def _ensure_excel_exists() -> None:
    if not os.path.exists(EXCEL_PATH):
        raise FileNotFoundError(
            f"Could not find Excel file at '{EXCEL_PATH}'. "
            "Set FOOD_CF_EXCEL_PATH or put sustainable_life.xlsx in the project root."
        )


def _load_food_dataframe() -> pd.DataFrame:
    """Load and normalize the Excel file."""
    _ensure_excel_exists()
    df = pd.read_excel(EXCEL_PATH, sheet_name=EXCEL_SHEET)

    if ITEM_COL not in df.columns or CF_COL not in df.columns:
        raise KeyError(
            f"Expected columns '{ITEM_COL}' and '{CF_COL}' in sheet '{EXCEL_SHEET}'. "
            f"Found columns: {list(df.columns)}"
        )

    cf_series = (
        df[CF_COL]
        .astype(str)
        .str.replace(",", ".", regex=False)
        .str.replace(" ", "", regex=False)
    )
    df[CF_COL] = pd.to_numeric(cf_series, errors="coerce")
    df = df.dropna(subset=[CF_COL])
    df = df.reset_index(drop=True)
    return df


def _build_langchain_vectorstore() -> FAISS:
    """Build LangChain FAISS vectorstore from Excel data."""
    global _df
    _df = _load_food_dataframe()
    
    embeddings = MistralAIEmbeddings(model=EMBEDDING_MODEL_NAME)
    
    if os.path.isdir(STORE_DIR):
        print(f"[RAG] Loading existing vectorstore from {STORE_DIR}...")
        vectorstore = FAISS.load_local(
            STORE_DIR, 
            embeddings, 
            allow_dangerous_deserialization=True
        )
    else:
        print("[RAG] Creating new vectorstore...")
        documents = []
        for idx, row in _df.iterrows():
            item_name = str(row[ITEM_COL]).strip()
            cf_value = float(row[CF_COL])
            
            doc = Document(
                page_content=item_name,
                metadata={
                    "index": idx,
                    "item_name": item_name,
                    "cf_kg_per_kg": cf_value
                }
            )
            documents.append(doc)
        
        # Process in batches to avoid API limits
        BATCH_SIZE = 64
        vectorstore = None
        
        for i in range(0, len(documents), BATCH_SIZE):
            batch = documents[i:i + BATCH_SIZE]
            print(f"[RAG] Processing batch {i//BATCH_SIZE + 1}/{(len(documents)-1)//BATCH_SIZE + 1}...")
            
            if vectorstore is None:
                vectorstore = FAISS.from_documents(batch, embeddings)
            else:
                batch_store = FAISS.from_documents(batch, embeddings)
                vectorstore.merge_from(batch_store)
        
        vectorstore.save_local(STORE_DIR)
        print(f"[RAG] Vectorstore saved to {STORE_DIR}")
    
    return vectorstore


def _lookup_items_batch(names: List[str], masses_g: List[float]) -> List[Dict[str, Any]]:
    """Batch lookup using LangChain similarity search."""
    global _vectorstore, _df
    
    if _vectorstore is None:
        raise RuntimeError("Vectorstore not initialized. Call warm_up_rag() first.")
    
    results: List[Dict[str, Any]] = []
    
    if not names:
        return results
    
    for name, mass_g in zip(names, masses_g):
        result: Dict[str, Any] = {
            "input_name": name,
            "matched_item": None,
            "mass_g": mass_g,
            "source": "unknown",
            "similarity_score": None,
            "cf_kg_per_kg": None,
            "emissions_kg_co2": None,
        }
        
        if not name or mass_g <= 0:
            result["notes"] = "Empty name or non-positive mass."
            results.append(result)
            continue
        
        try:
            search_results = _vectorstore.similarity_search_with_score(
                name, 
                k=1
            )
            
            if not search_results:
                result["notes"] = "No matches found in database."
                results.append(result)
                continue
            
            doc, distance = search_results[0]
            similarity = 1 - (distance / 2)
            
            result["similarity_score"] = similarity
            
            if similarity < SIMILARITY_THRESHOLD:
                result["notes"] = (
                    "Best match is not similar enough in the embedding space; "
                    "marked as unknown so the LLM may approximate from its own knowledge."
                )
                results.append(result)
                continue
            
            item_name = doc.metadata["item_name"]
            cf_kg_per_kg = doc.metadata["cf_kg_per_kg"]
            
            mass_kg = mass_g / 1000.0
            emissions = cf_kg_per_kg * mass_kg
            
            result["matched_item"] = item_name
            result["cf_kg_per_kg"] = cf_kg_per_kg
            result["emissions_kg_co2"] = emissions
            result["source"] = "database"
            
        except Exception as exc:
            result["notes"] = f"Error during lookup: {exc}"
        
        results.append(result)
    
    return results


def compute_meal_footprint(payload: str) -> str:
    """
    Tool called by Mistral via function-calling.
    
    Uses LangChain FAISS vectorstore for similarity search.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        error = {
            "error": f"Invalid JSON payload: {exc}",
            "raw_payload": payload,
        }
        return json.dumps(error)
    
    meal_label = data.get("meal_label", "meal")
    items = data.get("items", [])
    
    if not isinstance(items, list):
        error = {
            "error": "Expected 'items' to be a list.",
            "raw_payload": data,
        }
        return json.dumps(error)
    
    names: List[str] = []
    masses_g: List[float] = []
    
    for item in items:
        name = str(item.get("name", "")).strip()
        mass_g = float(item.get("mass_g", 0.0))
        mass_ml = float(item.get("mass_ml", 0.0))
        
        if mass_g <= 0 and mass_ml > 0:
            mass_g = mass_ml

        if not name or mass_g <= 0:
            continue
        
        names.append(name)
        masses_g.append(mass_g)
    
    batch_results = _lookup_items_batch(names, masses_g)
    
    total_emissions_db_only = 0.0
    any_unknown = False
    
    for res in batch_results:
        if res["source"] == "database" and res["emissions_kg_co2"] is not None:
            total_emissions_db_only += float(res["emissions_kg_co2"])
        else:
            any_unknown = True
    
    output: Dict[str, Any] = {
        "meal_label": meal_label,
        "items": batch_results,
        "total_emissions_kg_co2_database_only": total_emissions_db_only,
        "notes": "",
    }
    
    if any_unknown:
        output["notes"] = (
            "Some items could not be confidently matched to the database "
            "and are marked with source='unknown'. The LLM may approximate their CO2 "
            "using its own knowledge but should clearly explain this to the user."
        )
    
    return json.dumps(output)


def warm_up_rag() -> None:
    """
    Precompute the LangChain FAISS vectorstore at app startup.
    This uses LangChain's vectorstore abstraction as required.
    """
    global _vectorstore
    try:
        _vectorstore = _build_langchain_vectorstore()
        print("[RAG] LangChain FAISS vectorstore is ready.")
    except Exception as exc:
        print(f"[RAG] Warm-up failed: {exc}")
        raise