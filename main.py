import os
import gspread
import base64
import json
import uuid
from typing import List, Optional
from pydantic import BaseModel, HttpUrl, Field
from fastapi import FastAPI, Depends, HTTPException, status, Query, Request, Form
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.responses import JSONResponse
from google.oauth2.service_account import Credentials
from enum import Enum
from datetime import datetime, UTC, timedelta
from uuid import UUID
from fastapi.middleware.cors import CORSMiddleware

# --- Configuration & Setup ---
# Load credentials from environment variable and decode from base64
def get_spreadsheet():
    CREDS_JSON_BASE64 = os.environ.get("GOOGLE_SHEETS_CREDS_BASE64")
    if not CREDS_JSON_BASE64:
        raise ValueError("GOOGLE_SHEETS_CREDS_BASE64 environment variable not set.")
    CREDS_INFO = json.loads(base64.b64decode(CREDS_JSON_BASE64).decode("utf-8"))

    # Set up gspread client
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    creds = Credentials.from_service_account_info(CREDS_INFO, scopes=SCOPES)
    client = gspread.authorize(creds)
    SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
    if not SHEET_ID:
        raise ValueError("GOOGLE_SHEET_ID environment variable not set.")
    try:
        return client.open_by_key(SHEET_ID)
    except gspread.exceptions.SpreadsheetNotFound:
        raise ValueError(f"Spreadsheet with ID '{SHEET_ID}' not found.")

# --- Authentication ---
security = HTTPBasic()


def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    API_USER = os.environ.get("API_USER")
    API_PASSWORD = os.environ.get("API_PASSWORD")
    if not API_USER or not API_PASSWORD:
        raise ValueError("API_USER or API_PASSWORD environment variables not set.")
    if credentials.username != API_USER or credentials.password != API_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# --- Utility Functions ---
def get_worksheet(sheet_name: str):
    """Retrieves a worksheet by name, creating it if it doesn't exist."""
    spreadsheet = get_spreadsheet()
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=sheet_name, rows="100", cols="20")

def row_to_dict(headers: List[str], row: List[str]) -> dict:
    """Converts a row of data to a dictionary with given headers."""
    return {headers[i]: row[i] for i in range(len(headers))}

# --- Pydantic Models ---
class RSVPStatus(str, Enum):
    CONFIRMED = "confirmed"
    DECLINED = "declined"

class PersonType(str, Enum):
    ADULT = "adult"
    CHILD = "child"

class Companion(BaseModel):
    full_name: str
    person: PersonType

class RSVPRequest(BaseModel):
    full_name: str
    status: RSVPStatus
    phone: int
    companions: Optional[List[Companion]] = None

class RSVP(RSVPRequest):
    created_at: datetime


class GiftRequest(BaseModel):
    name: str
    image_url: str

class Gift(GiftRequest):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    available: bool = True
    purchased: Optional[str] = None
    updated_at: datetime

class GiftPublic(BaseModel):
    id: str
    name: str
    image_url: str

class GiftPurchaseRequest(BaseModel):
    id: UUID
    purchased: str

class TestimonialRequest(BaseModel):
    full_name: str
    message: str

class Testimonial(TestimonialRequest):
    updated_at: datetime

# --- FastAPI App ---
app = FastAPI(title="A&M Wedding")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)



# --- Endpoints ---

## RSVP
@app.post("/rsvp", status_code=status.HTTP_201_CREATED)
def register_rsvp(rsvp_data: RSVPRequest) -> dict:
    """Registers an RSVP confirmation."""
    ws = get_worksheet("rsvp")
    headers = ["full_name", "status", "phone", "companions", "created_at"]
    if ws.row_count < 2 or not ws.row_values(1):
        ws.update([headers])

    companions_json = json.dumps([c.model_dump() for c in rsvp_data.companions]) if rsvp_data.companions else None
    row = [rsvp_data.full_name, rsvp_data.status, rsvp_data.phone, companions_json, str(datetime.now(tz=UTC))]
    ws.append_row(row, value_input_option='USER_ENTERED')
    return {"message": "RSVP registered successfully."}

@app.get("/rsvp", status_code=status.HTTP_200_OK, response_model=list[RSVP])
def list_rsvps(status: RSVPStatus):
    """Lists RSVPs by status."""
    ws = get_worksheet("rsvp")
    records = ws.get_all_records()
    
    filtered_records = []
    for record  in records:
        if record.get("status") == status:
            record["companions"] = json.loads(record.get("companions")) if record.get("companions") else None
            filtered_records.append(RSVP(**record))

    return filtered_records

## Gift List
@app.post("/gifts", status_code=status.HTTP_201_CREATED)
def register_gifts(gifts: List[GiftRequest]) -> dict:
    """Registers a list of gifts."""
    ws = get_worksheet("gifts")
    headers = ["id", "name", "image_url", "available", "purchased", "updated_at"]
    if ws.row_count < 2 or not ws.row_values(1):
        ws.update([headers])

    rows = []
    for g in gifts:
        gift = Gift(name=g.name, image_url=g.image_url, updated_at=datetime.now(tz=UTC))
        rows.append([gift.id, gift.name, gift.image_url, gift.available, gift.purchased, str(gift.updated_at)])

    ws.append_rows(rows, value_input_option='USER_ENTERED')
    return {"message": f"{len(gifts)} gifts registered successfully."}

@app.patch("/gifts/purchased", status_code=status.HTTP_202_ACCEPTED)
def update_gift_purchased(purchase: GiftPurchaseRequest) -> dict:
    """Updates a gift's status to purchased."""
    ws = get_worksheet("gifts")
    cell = ws.find(str(purchase.id), in_column=1)
    if not cell:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gift not found.")

    # Update the row: set available to False and purchased to the buyer's name
    row_index = cell.row
    ws.update_cell(row_index, 4, False)
    ws.update_cell(row_index, 5, purchase.purchased)
    ws.update_cell(row_index, 6, str(datetime.now(tz=UTC)))

    return {"message": f"Gift {purchase.id} marked as purchased by {purchase.purchased}."}

@app.get("/gifts", status_code=status.HTTP_200_OK, response_model=List[GiftPublic])
def list_available_gifts(page: int = 1, limit: int = 10):
    """Lists available gifts with pagination."""
    ws = get_worksheet("gifts")
    records = ws.get_all_records()
    available_gifts = [record for record in records if record.get("available") == "TRUE"]
    available_gifts.sort(key=lambda x: x['name'])
    
    start = (page - 1) * limit
    end = start + limit
    paginated_gifts = available_gifts[start:end]
    
    return [
        GiftPublic(id=g['id'], name=g['name'], image_url=g['image_url']) 
        for g in paginated_gifts
    ]

@app.get("/gifts/{id}", status_code=status.HTTP_200_OK, response_model=GiftPublic)
def get_gift_by_id(id: str):
    """Retrieves a gift by its ID."""
    ws = get_worksheet("gifts")
    cell = ws.find(id, in_column=1)
    if not cell:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gift not found.")

    row = ws.row_values(cell.row)
    headers = ws.row_values(1)
    gift_dict = row_to_dict(headers, row)
    return GiftPublic(id=gift_dict['id'], name=gift_dict['name'], image_url=gift_dict['image_url'])
        

## Testimonials
@app.post("/testimonials", status_code=status.HTTP_201_CREATED)
def register_testimonial(testimonial: TestimonialRequest):
    """Registers a testimonial message."""
    ws = get_worksheet("testimonials")
    headers = ["full_name", "message", "created_at"]
    if ws.row_count < 2 or not ws.row_values(1):
        ws.update([headers])

    row = [testimonial.full_name, testimonial.message, str(datetime.now(tz=UTC))]
    ws.append_row(row, value_input_option='USER_ENTERED')
    return {"message": "Testimonial registered successfully."}

@app.get("/testimonials", status_code=status.HTTP_200_OK)
def list_testimonials(page: int = 1, limit: int = 10):
    """Lists testimonials with pagination."""
    ws = get_worksheet("testimonials")
    records = ws.get_all_records()
    
    start = (page - 1) * limit
    end = start + limit
    paginated_testimonials = records[start:end]
    
    return paginated_testimonials